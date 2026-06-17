# 多语言互翻执行方案 v3（执行基线）

- 状态：**立项，中高优先级。本文是唯一执行基线——「该建什么 / 什么顺序 / 逐 PR 编辑点」以本文为准。**
- 版本：**v3（2026-06-13，Asia/Shanghai）。取代 [v2](2026-06-13-multilingual-mutual-translation-plan.md)；v2 退为证据底座（§3 全量耦合表 + §12 核查记录仍有效，被本文引用）。**
- 来源：v2 + CodeX 审核（7 点）+ 独立 13-agent 复核（8 子系统逐条核实 + 5 维度对抗评审）合并。复核结论：约 50 条 v2 声明 **0 实质 refute**、3 个 blocker 全部坐实；但 v2 与 CodeX 合起来仍漏 8 条高危 + 2 处 blocker 机制描述错误，已在本文订正。
- 不变的核心判断（三方一致）：架构主干不阻止多语言互翻；真正阻力是**语言假设的静默耦合**，比 v2 自认的还多一层（音色池预筛、迁移顺序、第三写出器、成本子系统整块、验证/回滚单向性）。**绝不能压缩成「改语言参数」。**
- 首发 DoD 不变：**中文视频 → 英文配音（`zh-CN→en`），仅 Studio，禁 post-edit / suggest-split**；默认 `en→zh-CN` 为零回归基线。

---

## 0. v3 相对 v2 的变更清单（traceability）

| # | 变更 | 来源 | 落点 |
|---|---|---|---:|
| D1 | Studio 首发与 `requires_review` 暂停矛盾——必须二选一并写死 | CodeX#1 | §3、Phase 7、PR-A |
| D2 | `SemanticBlock` 口径订正（仍是 TTS/对齐/字幕输出 canonical block，非 legacy） | CodeX#2 | §2.1 |
| D3 | PR-E 改 `process.py:7726` 与 PR-W 独占冲突——重排领地 | CodeX#3 + 复核 | §5、Phase 5 |
| D4 | non-goal 需 Gateway + Job API 双层 gate（`enter-edit`/`suggest-split`） | CodeX#4 | §3、Phase 1/7 |
| D5 | `capabilities` 命名拆分（内部 `adapted_paid_capabilities` vs 前端 `workflow_capabilities`） | CodeX#5 | §2.2 |
| D6 | prompt override fail-closed 做成机器可校验契约 | CodeX#6 | §2.3 |
| D7 | Phase 6 拆「正确性阻断 vs 排版质量」，与 DoD 对齐 | CodeX#7 | Phase 6、§6 |
| **A** | **MiniMax 音色池在 `VoiceMatchRequest` 之前预筛中文 → 接 `target_language` 是 no-op** | 复核(架构, high) | Phase 5、§1 |
| **B** | **matchable 回填会先武装 legacy `:160` 过滤器（无 switch/无语言谓词）→ flip 前泄露 en 音色** | 复核(顺序, high) | Phase 5、§1 |
| **C** | **第三个 zh 硬编码 `jianying_draft_runner.py:1497` + `editor_package_writer.py:369-371` 也是写出器（共三个）** | 复核(完整性, high) | Phase 6、§1 |
| **D** | **CosyVoice fallback = 2026-04-05 自动克隆事故同型（未选择付费调用 + 中文音色读英文）→ 付费硬约束** | 复核(付费, high) | §2.4、Phase 5 |
| **E** | **`billing_chars.py` + `cost_management.py` 不在 §3、无语言维度 → Phase 8「cost per minute by pair」做不出** | 复核(完整性, high) | §1、Phase 1/8 |
| **F** | **fingerprint hash 的正是 Phase 3/4 要改的函数输出 → 比 v2 说的更脆** | 复核(回归, high) | §2.5、Phase 3 |
| **G** | **golden snapshot 只录 en→zh，测不到 zh-source 静默退化；shadow 锁默认 pair 产不出 zh→en 信号** | 复核(回归, high) | §5、Phase 0 |
| **H** | **「只回滚 config」对默认 pair 是单向门：kill switch 关不掉 GA en→zh** | 复核(回归, high) | §5 |
| X1 | v2 §3.7 blocker 机制描述错误（refuted）：剪映/materials 非「从 segments.json 重建 cue」 | 复核(refute) | §1、Phase 6 |
| X2 | `final_cn_chars=len(cn_text)` 把英文按中文计费——v2 的注释级修复不够 | 复核(refute partial) | §2.4、Phase 1 |

> 另有一组 MEDIUM 静默退化耦合（复核确认，进各 PR fixture）：`_rough_spoken_word_count` 漏所有非 CJK 非 Latin 文种、`_count_cn_chars` 对 Latin 标点缩水、`GeminiRewriter` cps=4.5 写死中文速率、`duration_estimator` 英文时长误差、`SegmentRow` 行高 ÷40(中)/÷80(英)、`BulkReplacePreviewResponse.field` 字面量类型 `"cn_text"`（改名需新 union 成员）。

---

## 1. v3 新增 / 订正耦合点（v2 §3 之外，必须进对应 PR）

> v2 §3 的全量耦合表（English 门、translator/cn_text、ASR、S2 glossary、字幕 per-script、matchable 硬过滤、匿名 lane 等）已独立复核确认，仍是 PR 的依据。下表只列 v2 漏掉或描述错误的，severity 针对首发 `zh-CN→en`。

| 耦合点 | 证据 | severity | 处置 |
|---|---|---|---|
| **[A] MiniMax 音色池在请求构造前预筛中文** | [process.py:7824](src/pipeline/process.py:7824) `v.get('language') not in ('中文-普通话','中文-粤语')`，在 `_build_voice_selection_review_payload`（[:7726](src/pipeline/process.py:7726)）内、`_auto_match_for_provider` 构造 `VoiceMatchRequest` **之前**执行 | **high(no-op 陷阱)** | 去中文池是「接 target_language」的**前置**，非并行；验收测「池」非「resolver」 |
| **[B] matchable 回填先武装 legacy 过滤器** | [voice_catalog_api.py:160](gateway/voice_catalog_api.py:160) `matchable == True` 无 kill switch、无语言谓词；回填瞬间 36 行 en 即对其可见 | **high(顺序)** | 见 Phase 5 重排顺序；kill switch 必须也门住 legacy `:160` 查询路径 |
| **[C] 第三个字幕 zh 硬编码 + 三写出器** | 剪映 [jianying_draft_runner.py:1497](src/services/jobs/jianying_draft_runner.py:1497) 无条件取 `editor.subtitles`(zh alias)；写出器实为三个：`ensure_whisper_alignment.py:404-410` + [editor_package_writer.py:369-371](src/modules/output/editor/editor_package_writer.py:369)(`subtitles.srt=zh copy`) + 任何 legacy | **high** | Phase 6 把这两处列为显式编辑点（v2 只列了 ensure_whisper_alignment） |
| **[D] CosyVoice fallback 语言盲 + 付费** | [tts_generator.py:1219-1248](src/services/tts/tts_generator.py:1219) minimax/volc 失败静默路由付费 cosyvoice；selector 返中文 `longanyang`（[cosyvoice_voice_selector.py:107](src/services/tts/cosyvoice_voice_selector.py:107)，gender=None 时无条件）| **high(付费硬约束)** | 非默认 pair fail-closed（不静默路由 + 不读英文），见 §2.4 |
| **[E] 成本子系统无语言维度** | `mainland_worker/billing_chars.py` CJK=2/其他=1 计费（英文输出按 1/char、零 CJK 权重，与 en→zh 不可比）；`cost_management.py:1023/1197` cost_per_minute 无 pair 维度 | **high** | §3 补这两个耦合行；Phase 1/8 加 `language_pair` 聚合维度 |
| **[X2] `final_cn_chars` 把英文当中文计费** | [process.py:1697](src/pipeline/process.py:1697) `total_cn_chars += len(cn_text)`；zh→en 时 cn_text 是英文，`len()` 计 Latin 字符 | medium | 结构性修复（新增 typed 字段），非注释，见 §2.4 |
| **[X1] v2 §3.7 blocker 机制描述错误** | 剪映/materials **不**从 segments.json 重建 cue：`_build_jianying_request` 读 manifest `artifact_index`，materials 用 `resolved.name`；真实机制=`ensure_whisper_alignment` 覆盖磁盘 SRT + 下游按 manifest 的 zh-alias key 取文件 | (订正) | blocker 结论成立，但 PR-F 须打对靶（覆盖+key 选择，非 cue 重建） |

---

## 2. 架构裁决（v2 §4 的订正与补强）

### 2.1 SemanticBlock 口径订正（D2）

v2 C1 措辞「SemanticBlock 是 legacy/cue 路径」会误导后续 PR 删/绕它。订正为:
- `DubbingSegment.cn_text` 是**当前持久化目标文本容器**;
- `SemanticBlock` 仍是 **TTS/对齐/字幕输出的 canonical block**（[process.py:10583](src/pipeline/process.py:10583) `_build_process_output_blocks(...) -> list[SemanticBlock]` 仍在输出路径构造）。
- **不变量:任何 PR 不得删/绕 SemanticBlock 输出路径。**

### 2.2 `capabilities` 命名拆分（D5）

避免内部适配位与前端展示位同名:
- 内部 `LanguagePairProfile.adapted_paid_capabilities`（`{probe, s2, suggest_split, post_edit}`，驱动 §2.4 付费 gate）。
- Gateway facts 前端字段 `workflow_capabilities`（`["transcribe","translate","tts","subtitles","jianying"]`，展示用）。
- 二者代码层面不复用同一常量名。

### 2.3 prompt override fail-closed 做成机器可校验契约（D6）

v2「override 不含 language-aware 声明就回默认」是方向，需落成 checkable 契约。对 **Translator + probe + S2 三个 override key（pass1/2/3）**统一:
- 非默认 pair 的 override 必须显式声明 `source_language`、`target_language`、`output_schema_version`，并含 `target_text` schema 占位符;缺任一 → fail-closed 回 registry 默认模板，不执行。
- 现状仅 [translator.py:1867-1872](src/services/gemini/translator.py:1867) `validate_translation_prompt_template` 校验 `__GROUPS_JSON__`;S2/probe override 路径([transcript_reviewer.py:1247/1638/1991](src/services/transcript_reviewer.py:1247) 无条件覆盖)**当前无校验器**——PR-H 须为每个 override key 加同形校验器。
- 验收:`test_paid_api_capability_gate` 断言非默认 pair 上「不声明语言」的 S2 override 被拒/忽略,不被执行。

### 2.4 付费 API 语言错配 fail-closed（D，X2，项目硬约束）

总闸 = `adapted_paid_capabilities` per-capability 适配位。补两条 v2 漏的:
- **[D] CosyVoice fallback:** `get_fallback_provider`（[tts_strategy.py:64-78](src/services/tts/tts_strategy.py:64)）+ TTSGenerator 自动切（`:1219-1248`）对非默认 pair **fail-closed**——不静默路由到中文-only CosyVoice、不让中文音色读英文。这是 **2026-04-05 自动克隆事故同型**（未经用户选择的付费调用 + 错语言产物），按 CLAUDE.md 硬约束处理,不是「改语言感知就行」。
- **[X2] metering 结构性修复:** 不靠 v2 §4.4 的注释。`record_tts`（[usage_meter.py:118-131](src/services/usage_meter.py:118) 无 `extra`）**改签名加 `language_pair: str=''`**（与已上线的 `record_llm` extra 模式一致,`summarize()` 忽略未知键,安全）;`_build_job_metering_payload`（process.py:1595）**新增 `final_target_units` + `target_unit_kind` typed 字段**,Phase 8 cost 指标只读 typed 字段,`final_cn_chars` 冻结为默认-pair 含义。三处 metering 写入点（`record_tts` / `metering_snapshot` [job_intercept.py:1699](gateway/job_intercept.py:1699) / pipeline payload）口径统一。

> `target_text` 单写裁决（v2 §4.3）**不变且正确**:`cn_text` 保持 canonical 目标文本容器、`target_text` 仅作 parser 别名 + 导出层、不进 persistence/不进 post-edit 白名单;字段重命名延到独立原子 sweep PR。复核独立确认 `cn_text` 392 处/50 文件 + `cn_text != tts_input_cn_text` drift 契约,双写会损坏该契约。匿名 lane 靠 JobRecord 缺省锁定、白名单不动,也维持。

### 2.5 fingerprint default-pair-preserving 强化（F）

v2「锚定默认 pair 到旧算法」低估了风险:[translator.py:803-811](src/services/gemini/translator.py:803) 的 fingerprint **hash 的正是** `_count_source_words`/`×1.8`/`_estimate_dynamic_target_chars` 的输出——这些是 Phase 3/4 要 script-dispatch 的函数。共享英文路径上任何 ULP 级 rounding / median 漂移都会 silently busts 全部现网 en→zh checkpoint → 付费重翻。订正:
- 冻结一份 `_build_translation_fingerprint_v1` 给 `en→zh-CN` **逐字节复用**;新字段只在非默认 pair 以独立 hashed sub-key 追加;`TRANSLATION_CHECKPOINT_VERSION` 仅为新 schema bump。
- 回归测试钉死到含 **(a) 慢速低-wps 段、(b) 3+ 说话人 reference-wps median、(c) 零-source-word 段** 的 fixture（最易 round-drift 的三条数值分支）。
- 默认 pair hash 变化 = **release blocker**,非 warning。

---

## 3. 分阶段执行计划（v3）

> 工期:维持 v2 的 4-6 周（单 owner）量级,但 §5 PR 重排后关键路径更长（见 §4）。Phase 5/6 是失真高发区。

### Phase 0：能力矩阵 + 测试夹具 + 两类 golden（2-3 天，扩容）
1. `docs/research/multilingual-provider-capability-matrix.md`（ASR/Translator/TTS/Whisper/subtitle per-language;含 AssemblyAI disfluencies/diarization、VolcEngine explicit_language 实测）。
2. 离线 fixture:en→zh 现有 + zh-CN→en 2-3 段中文短访谈。
3. **(G) 两类 golden 分录:** ① en→zh **byte/结构 golden**（默认-pair 非回归证明）;② zh→en **behavioral golden**（正向输出断言:`source_word_count>0`、`reference_wps>0`、probe 非空、目标字幕 Latin、剪映草稿英文）。
4. **(G+完整性) zh→en ratio 0.55 实测:** 0.55 是 2026-04-15 旧方案的「约 0.55」估计,**必须从 fixture clip 测出再喂任何长度预算**,不是上线后再校。

### Phase 1：语言事实入库 + API 贯通 + 灰度开关 + 双层 gate（3-4 天）
1. alembic 036 + models lockstep + JobRecord 字段（NOT NULL server_default `en`/`zh-CN`/`en->zh-CN`）。
2. **三个 field-by-field Job 构造点全部显式加字段** + **AST/reflection 守卫**:PG insert（[job_intercept.py:1634](gateway/job_intercept.py:1634)）、metering_snapshot（`:1699`）、**copy_as_new（[:4747](gateway/job_intercept.py:4747)）**——守卫断言任何 Job/JobRecord 构造器漏列三字段即 red(防 `feedback_copy_as_new_invariants` 静默回落)。
3. entitlements `get_effective_allowed_language_pairs` + admin_settings `language_pairs_enabled`/allowlist + **admin 前端开关 UI** + **(D4) Gateway 与 Job API 双层 pair gate**(`enter-edit`/`suggest-split`/各 editing mutation 在 [jobs/api.py:1071](src/services/jobs/api.py:1071) service 层也拒非默认 pair,非仅 Gateway)。
4. **匿名 lane:** [anonymous_preview_payload_spec.py:14-35](gateway/anonymous_preview_payload_spec.py:14) 白名单**不动** + 回归断言「不含语言字段」+「匿名 job 恒解析 en→zh-CN」。
5. **(E) cost 维度:** `metering_snapshot` + `cost_management.py` 聚合加 `language_pair`(否则 Phase 8 算不出 cost per minute by pair)。
6. 前端 `CreateTranslationJobInput` 可选语言字段;facts 端点(返 `workflow_capabilities` + `label`);Job summary 返语言。
7. **部署 runbook 进验收**(migration 先行 → gateway/app 滚动;`--sql` dry-run;compose env 变更先查 in-flight pipeline,`feedback_compose_env_file_recreate`;gateway 需 `PYTHONPATH=app/`,`feedback_apf_deploy_incident`)。

### Phase 2：source-aware gate + ASR 参数化（2-3 天）
1. `_enforce_english_*`（[process.py:8099/8111](src/pipeline/process.py:8099)）→ source-aware,**保留 metadata 缺失/auto 放行(fail-open)+ 回归**(本地上传无 metadata 全挂的防回归)。注:此为 process.py 编辑点,归 PR-W(见 §4)。
2. AssemblyAI:language_code 参数化 + **per-language disfluencies/filler/speech_models/diarization profile**。
3. AssemblyAI 转录行构建:`SENTENCE_END_PATTERN`/`_join_tokens` 按 script family 分派(CJK 全角标点 + 无空格连词;去英文缩写 `'` 特例)。
4. Gemini transcriber prompt 加 source language;`TranscriptResult.language` 失败改 fail-closed(不静默回 en)。

### Phase 3：Translator + Rewriter + Probe 语言化（3-5 天）
1. language_pair profile + prompt registry(translation + probe + rewrite)。
2. parser 兼容 `target_text/cn_text`;canonical 写 `cn_text`。
3. **`_count_source_words` 按 source script 分词** + reference wps 机制随之。
4. **长度三件套统一换 spoken unit:** `_estimate_dynamic_target_chars`(5 处 ×1.8) + `_count_cn_chars` 重试 gate + rewriter 字数口径(en target ~5× 偏差)。
5. **GeminiRewriter 3 条 prompt 路径全覆盖**(compact + 硬编码中文尾巴 + cps=4.5 中文速率默认改 per-language)。
6. **(F) fingerprint:** 冻结 `_v1` 给默认 pair 逐字节复用 + 非默认 pair 独立 sub-key + 数值边界 fixture 钉死(见 §2.5)。

### Phase 4：长段修复 + voice speed 单位（2-3 天）
1. 长段修复 split 正则按 source/target script 分派([process.py:342-343](src/pipeline/process.py:342))。注:process.py 编辑点,归 PR-W。
2. voice_reranker speed 维度读 target unit;**zh→en 首发明确 DISABLE speed 维度**(catalog cps 是 hanzi/sec、与 word/sec 不可比;DSP/rewrite 承担全部时长适配),per-language cps 重校准延后——并在 §6 DoD 标为首发已知限制。

### Phase 5：音色兼容 + matchable 原子迁移（4-6 天，重排）
1. **(A) 去中文池是前置:** 先 de-Chinese [process.py:7726/7824](src/pipeline/process.py:7726) 音色池 builder(归 PR-W),**再**让 VolcEngine/CosyVoice selector([voice_match_resolver.py:79-109](src/services/tts/voice_match_resolver.py:79))消费 target_language;否则接 target_language 是 no-op。
2. **(B) matchable 迁移顺序订正:** ① 先回填 `compatible_target_languages`、保持 `matchable=false` → ② 把运行查询([voice_catalog_api.py:160](gateway/voice_catalog_api.py:160))切到按 target 兼容过滤(kill switch 同时门住此 legacy 查询)→ ③ 验证 zh target 返 0 en 行 → ④ 才置 `matchable=true`。**每一步加回归断言 zh target 不返 volcengine en。**独立 admin kill switch,关闭即回 legacy。
3. **(D) 跨 provider fallback fail-closed**([tts_strategy.py](src/services/tts/tts_strategy.py) + [cosyvoice_voice_selector.py:107](src/services/tts/cosyvoice_voice_selector.py:107) 无 gender 兜底)。
4. TTS 合成 payload 注入语言参数(MiniMax `language_boost` / VolcEngine `explicit_language`;默认 speaker 别恒 `zh_female_*`)。
5. **user_voices 语言 migration(v2 未列)** + 3 条写入路径 stamping(手动 clone / Express auto-clone [pipeline_clients.py:110-135](src/services/express/pipeline_clients.py:110) / Smart auto-clone)。
6. DubbingSegment/segments.json 加 per-segment `target_language`([tts_generator.py:1454](src/services/tts/tts_generator.py:1454) getattr 已 wired,只缺数据)。

### Phase 6：字幕正确性阻断（MVP）+ 排版质量（GA 延后）（1-1.5 周，拆分 D7）
**MVP（正确性阻断,首发必做）:**
1. **(C) 三个 zh 写出器/选择点同步:** ① `ensure_whisper_alignment.py:404-410` 不写死 `subtitles_zh.srt`;② 剪映 [jianying_draft_runner.py:1497](src/services/jobs/jianying_draft_runner.py:1497) 非默认 pair 取目标语言字幕 artifact,非 zh alias;③ [editor_package_writer.py:369-371](src/modules/output/editor/editor_package_writer.py:369) `subtitles.srt=zh copy`。
2. **(X1) 打对靶:** 真实机制是覆盖磁盘 SRT + manifest zh-alias key 选择,非 cue 重建——PR-F 改这两类,别去找「segments.json 重建」。
3. 非默认 pair **bypass 易出错的字符级 DTW**(whisper 强制 `language="zh"` [cue_pipeline.py:240](src/modules/subtitles/cue_pipeline.py:240) 对英文音频会产垃圾时间戳),用够读的词级切分保证**内容正确**;canonical cue / dispatcher 写 source/target;新增 `subtitles_source/target.srt`;en→zh 继续写旧 `subtitles_zh/en.srt`。
4. **artifact key 同步面 ≥9 处**(v2 §3.7 已枚举,复核确认)+ 回归守卫;下载文件名(RFC 6266)+ R2 registry 持久化按 target 生成,不写死 `_zh`。

**GA 延后（排版质量,非首发,与 DoD「只承诺内容正确」对齐）:** per-script semantic_segmenter / cue_timing / 词级 DTW、Latin 断行最优、剪映轨名 `zh_subtitle`、字幕宽度 CJK 预算。

### Phase 7：前端灰度 UX（1-2 天，不含 review UI）
1. TranslationForm 语言方向 selector(默认「英文 → 中文」)；非 GA pair「内测」标识。
2. 下载列表标签/描述由语言字段驱动(`mappers.ts` 标签 + 描述 + `SegmentRow` ÷40/÷80 行高 script-aware)。
3. 服务模式卡片按 pair 刷新;切换 pair 清不兼容音色 + consent。
4. **(D1) 首发 Studio lane 决策(见 §3 决策框):** 若走非交互 lane → 前端隐藏该 pair 的 voice_selection_review 入口;若保留人工兜底 → 该 review UI 须先语言中立化(则不再属「不含 review UI」)。

> **review UI 语言中立化**(cnText 标签 + 英文词边界拆分 [SplitSegmentDialog.tsx](frontend-next/src/app/(app)/workspace/[jobId]/edit/SplitSegmentDialog.tsx) + `BulkReplacePreviewResponse.field` 字面量类型)是 **GA 前置独立工作项**,不在首发;首发经 §non-goal 禁入 post-edit/suggest-split。

### Phase 8：灰度上线 + 数据闭环（持续）
顺序:Internal Studio zh→en → Allowlist Studio → Express(TTS 兼容覆盖后)→ Smart(shadow 稳定后)→ 更多 pair。
观测(落点见 §2.4 typed metering + Phase 1 cost 维度):翻译长度超限率、TTS first-pass duration error、DSP/rewrite/force_dsp 分布、wrong-script rate、voice fallback rate、cost per minute **by pair**(依赖 §2.4 + Phase 1.5)。

> **【首发 Studio lane 决策框 — D1，已定 2026-06-13:非交互 lane】** 现网 Studio `requires_review=True`([job_intercept.py:527](gateway/job_intercept.py:527)),pipeline 在 [process.py:3696](src/pipeline/process.py:3696) voice_selection_review 暂停。
> **裁决:首发 zh→en 走非交互 lane** —— 内部任务 `requires_review=False` + 自动选兼容 en 音色,绕开「review UI 中立化」GA 前置依赖,最短拿到可跑垂直切片。
> 落地:PR-A 的 effective config 对 `zh-CN→en` 强制 `requires_review=False`(与现网 Studio `requires_review=True` 解耦,仅此 pair);前端隐藏该 pair 的 voice_selection_review 入口(Phase 7 step 4)。保留人工兜底方案(review 开着 + UI 中立化)作为 GA 备选,不在首发。

---

## 4. PR 拆分与合并顺序（v3 重排）

**核心订正(D3 + 复核 critical):** v2「B/H/E/F 文件领地不相交」是伪命题——Phase 2/4/5 至少 5 处 `process.py` 编辑点(ASR 调用、两个 gate、音色池 builder + MiniMax 过滤、第二份 ×1.8、`_count_source_words`)落在 PR-W 独占文件里。**裁决:PR-W 扩权,拥有 process.py 内所有 language-aware 编辑(非仅 no-op),behavior 全部 gate 在 kill switch 后使默认 pair 逐字节不变;B/H/E/F 缩成真正非-process.py 的 PR。**

| PR | 范围 | 文件领地 | 依赖 |
|---|---|---|---|
| **PR-A** | language registry + Job 字段 + Gateway facts/校验 + 双层 gate + 灰度开关 + admin UI + migration 036 + 三构造点守卫 + 匿名断言 + cost 维度 | gateway + 前端 types + registry | — |
| **PR-W** | **process.py 内全部 language-aware 编辑**(gate + 音色池 builder + MiniMax 过滤 + 5 处 ×1.8 + `_count_source_words` 调用 + ASR 调用点),kill switch 后默认 pair byte-identical | `process.py`(**独占**) | A |
| **PR-CD** | translator + rewriter + probe 语言化 + parser + **fingerprint `_v1` 冻结(F)** + 长度三件套 + source 分词器 + cps per-language | `translator.py`+`rewriter.py`(**独占**) | W,**H**(glossary 方向契约) |
| **PR-B** | ASR per-language profile + 转录行 script 分派(transcriber 文件;process.py 调用点已由 W 铺好) | `assemblyai/`+`gemini/transcriber.py` | W |
| **PR-H** | S2 三轮 prompt + **glossary 方向** + S2/probe override fail-closed 校验器(§2.3) + legacy 死代码裁决 | `transcript_reviewer.py` | W |
| **PR-E** | selector 消费 target_language + **matchable 顺序迁移(B)** + **fallback fail-closed(D)** + 合成 payload 语言参数 + user_voices migration | `tts/`+gateway voice_catalog | W |
| **PR-F** | 字幕 MVP:三写出器/选择点(C) + bypass 字符级 DTW + artifact key ≥9 处 + 下载文件名 | `subtitles/`+output+下载层 | W |
| **PR-G** | 前端灰度入口(不含 review UI)+ lane 决策落地 | 前端 | A,F |

**合并顺序:** `A → W → {B、H 并行} → CD → E → {F、G(前端) 并行}`。
- 真正并行:`{B,H}`、`{F, G 前端}`。
- 串行关键路径:`A → W → CD → E`(W 因扩权约 1-1.5 周;CD 依赖 H 的 glossary 方向契约;E 依赖 W 的池 builder)。
- **glossary 方向(H↔CD 数据契约):** 优先在 `LanguagePairProfile`/language_registry(PR-A) 定为 typed 字段,H 与 CD 都读、都不硬编码方向;否则 CD 显式依赖 H。

**多 agent 硬约束:** `process.py` 归 PR-W、`translator.py`/`rewriter.py` 归 PR-CD,期间他人不碰;各 agent 自己 worktree + feature 分支,项目主 review 合并。

---

## 5. 零回归 + 回滚（v3 强化）

**零回归(v2 四件套 + G/F 补强):**
1. **(G) 两类 golden 分清职责:** en→zh **byte golden**=默认-pair 非回归;zh→en **behavioral golden**=新路径正向输出断言。**byte golden 结构上测不到 zh-source 静默退化(那只在中文源时显形),该保护落在新无网络单测**(`test_source_word_count_by_script` 等),别把两种保证混在「snapshot」一词下。
2. fingerprint 稳定性测试(§2.5,默认 pair 钉死录制哈希 + 数值边界 fixture)。
3. set-diff 基线对照(本机约 335 条预存失败,`feedback_test_database_stub_convention`)。
4. **(F) Phase 6 golden 逐文件字节:** 断言确切文件名集 + 每文件字节 + 剪映草稿字幕文本(en→zh fixture,PR-F 前后),非仅 set 成员。
5. **shadow 期只证 en→zh 不回归**(锁默认 pair → 无 zh→en 流量)。**(G) zh→en 质量另设 pre-allowlist 门:** 定 rubric(N 内部 clip,人评翻译保真 + wrong-script + 单位换算后 duration drift)、通过阈值、签字人——shadow 产不出 zh→en 信号。

**回滚(H 单向门订正):**
- 回滚**只动 Gateway pairs + admin kill switch**(含 PR-E 新过滤独立开关);代码与 DB 前滚不 revert(理由:新 fingerprint 任务旧代码 resume 必失配付费重翻;matchable 是 DB 状态,revert 代码复发 2026-04-15)。
- **(H) 默认 pair 单向门:** kill switch 关不掉 GA en→zh。若 PR-CD 伤到默认 pair 且 fingerprint 测没抓到——**把新 translator 路径包在 runtime flag 后、en→zh 回落到逐字节冻结的旧 translator**,让默认 pair 回归也 config-可回滚。PR-CD/PR-E 视为单向门,合并前 golden + behavioral + 数值 fixture 全绿是硬门。
- **queued 取消退款**走 `mirror_job_terminal_state` + `settle_job_credit_ledger` 单一终态入口(`feedback_terminal_state_single_entry`);加回滚路径测试:in-flight queued 非默认-pair job + flip switch → 断言落终态 + 退款结算。
- 回滚不需 DB downgrade(字段 additive)。**但 shadow 回滚决策依赖的 metering 必须按 §2.4 typed 化**,否则 `final_cn_chars` 跨 pair 不可比、污染 rollout 决策指标。

---

## 6. 最小可上线定义（v3 收窄）

1. **en→zh-CN 零回归** = byte golden + fingerprint 钉死 + Phase 6 逐文件字节 + shadow 三重证明。
2. **zh-CN→en 在 Studio 非交互主干**完成:转录 → 翻译 → TTS → DSP 对齐 → 源/目标/双语字幕 → 剪映草稿;**(G) 经独立 pre-allowlist 人评门**(shadow 不覆盖 zh→en)。
3. 不兼容音色不进候选池(**(B) matchable 顺序迁移 + (A) 池 builder 去中文,二者皆达**才算)。
4. **(D) 非默认 pair 无未经选择的付费调用**(CosyVoice fallback fail-closed)。
5. 质量报告识别目标语言脚本异常(shadow)。
6. Gateway 能关闭该 pair(双层 kill switch,且门住 legacy `:160` 查询)。
7. 默认测试不依赖真实外部 API。
8. **non-goals 经 Gateway + Job API 双层 gate 拒绝**(D4),对内测用户显式声明:禁 post-edit/suggest-split,字幕只承诺**内容正确**(Latin 排版/词级 DTW 属 GA)。
9. **首发已知限制(显式声明):** zh→en voice speed 维度 DISABLE(cps 不可比,DSP/rewrite 承担时长适配)。

达成后再讨论 Express / Smart / Free / Anonymous 与 review UI 中立化 + 字幕 GA 排版质量。

---

## 7. 证据与可追溯

- v2 [§3 全量耦合表 + §12 核查记录](2026-06-13-multilingual-mutual-translation-plan.md) 仍是依据,独立复核确认约 50 条声明 0 实质 refute、3 blocker 坐实。
- v3 新增/订正项的 file:line 已内联本文 §1/§2/§3。
- 复核覆盖:8 子系统逐条核实(process / translator / asr / s2 / tts-voice / subtitles / gateway-db / frontend)+ 5 维度对抗评审(architecture / sequencing / zero-regression-rollback / completeness / paid-api-cost)。
- 三个 blocker 主程亲验锚点:`voice_catalog_api.py:160`、`editing_segments.py:98`(`PATCHABLE_SEGMENT_FIELDS` 含 `cn_text`+`source_text`+`speaker_id`,无 `target_text`)、`ensure_whisper_alignment.py:404-410`。
