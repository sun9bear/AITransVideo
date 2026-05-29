# 免费版视频翻译流程设计（MiMo voiceclone 保留原声）

**日期**：2026-05-29
**状态**：`PHASED` —— 经 CodeX review 后改为**两阶段**（见 §1.5）。**Phase 1（内部 spike / allowlist 灰度）先行**，验证 MiMo voiceclone 质量/延迟/失败率/参考片段/10MB/成本；**Phase 2（公开免费版）** 待 Phase 1 通过 + 补齐落地 gate + consent/法务确认后再做。**【2026-05-29】Phase 1 批量验收已通过（4/4 成功、p50≈10s、3 段复测均无口吃），转入 Phase 2 规划。另测得 MiMo voiceclone 长输入 run-to-run 不稳定（同输入时长/停顿方差大）；产品决策：免费版本期先不做切片（现有 Express 对齐吸收方差、免费版可接受波动），切片降为 deferred 后备杠杆（gate #7，见 §1.5）。** brainstorming 全段确认（1/1b/2/3）+ spec review 过；CodeX review 6 点已核实属实并吸收。
**类型**：设计稿（design spec，brainstorming 产物）
**前置**：[MiMo voiceclone/voicedesign 可行性评估](2026-05-29-mimo-voiceclone-voicedesign-feasibility.md)

> ⚠️ 本文档随 brainstorming 推进逐段补全，settle 后才跑 spec-document-reviewer + 用户 review + writing-plans。当前为活文档草稿。

## 0. 目标

新增一个**独立于快捷版/工作台版/智能版**的"免费版"视频翻译流程：把每个段落的**原始说话人音频（demucs 分离后的干净人声）作为参考**，连同翻译后的中文文本发给 **MiMo `mimo-v2.5-tts-voiceclone`**，zero-shot 生成**保留原说话人音色**的中文配音。作为 **freemium 引流漏斗**。

## 1. 已确认的产品决策（brainstorming）

| 决策 | 结论 |
|---|---|
| 目的 | **免费引流 / freemium 漏斗**（吸引用户体验"保留原声"翻译，再升级付费） |
| 成本边界 | 单条 **≤ 10 分钟**；每用户 **每天 1 次**；输出 **轻量水印** |
| 水印 | **admin 后台可配**：文字内容、位置、大小、透明度 |
| kill-switch | MiMo 转收费时**降级到最便宜的预设引擎（CosyVoice）**，免费版继续（丢"保留原声"卖点，平台吃掉被 10min/1天 封顶的小额 TTS 成本） |
| 架构 | **方案 A**：新建 `service_mode="free"`，**复用 Express 非交互管线** |

> 关键现实记录：**"免费版"对平台不是零成本**——即使 MiMo TTS 免费，每条仍有 ASR（AssemblyAI 付费）+ LLM 翻译/rewrite（付费）+ demucs 算力 + 存储带宽。10min + 1/天是控住这部分成本敞口的核心闸门。

## 1.5 执行分期 + 落地集成要求（2026-05-29 CodeX review 后修正）

CodeX review 指出"复用 Express"被想得太轻——多个下游系统对未知 service_mode **默认成错误行为**。已逐条核实属实（带行号）。据此改为**两阶段执行**：

### Phase 1：内部 spike / allowlist 灰度（先行，不建公开脚手架）
目标：用真实视频验证 **MiMo voiceclone 的核心价值**，再决定是否值得建 freemium。验收：
- voiceclone 中文配音**质量 / 音色一致性**（per-speaker 参考 vs per-segment）
- **参考片段提取**（从 `speech_for_asr.wav` 重切最长干净片段，§2.2）
- **延迟 / 失败率**、**10MB base64 上限**命中情况、**成本观测**（promotional 计量）
- 仅 admin / allowlist 用户可见；**不**接公开入口、不动 quota/pricing/下载 gate
- 这也顺带满足合规上的"先 beta 不全量"（§5.3）

**Phase 1 首测结果（2026-05-29，美国主机真实任务）**：
- ✅ **调用打通**：真实任务 `job_e6067174347645209e7d75040599ed3b` seg1（speaker_a），从 `speech_for_asr.wav` 切 4s 干净原声做参考（16kHz mono，base64 170KB）+ `cn_text` → `mimo-v2.5-tts-voiceclone` 返回有效中文 WAV（7.52s，361KB），延迟 **6.2s**，usage{prompt:76,completion:49,cached:9}。
- ✅ **质量**：用户试听判断"还行，甚至比 CosyVoice 克隆好"——核心卖点（保留原声）成立。
- ⚠️ **延迟方差**：同任务先用 7s 参考（base64 456KB）**120s 超时**，换 4s 参考即 6.2s。非线性，叠加美国→中国 api.xiaomimimo.com 跨境延迟。**结论：参考片段控制在 3–5s；跨境可靠性/失败率需小批量复测**（Phase 1 待办）。
- 验证机制：assistant role + `cn_text`、`modalities:["audio"]`、`audio.voice=data:audio/wav;base64,...`、`format:wav`。

**Phase 1 批量结果 + 验收结论（2026-05-29，美国主机，同一真实任务 `job_e6067174347645209e7d75040599ed3b`）**：
- ✅ **失败率**：单说话人视频 seg1–4 批量跑 voiceclone，**4/4 成功、0 失败**。1 份 per-speaker 参考（`speaker_a`，从 `speech_for_asr.wav` 提取的 ≤5s 干净片段）喂全部 4 段，**音色段间一致**（用户试听未提漂移）——验证 **per-speaker（而非 per-segment）参考策略**成立。
- ✅ **延迟**：5.5 / 10.0 / 12.3 / 9.5s，**p50≈10s、max 12.3s**，均 < 15s 门槛；参考封顶 5s，**未撞 10MB 上限**。
- ⚠️ **质量（长文本 run-to-run 不稳定）**：seg3（原段 29.55s、中文 126 字，约 seg1 的 4 倍长）首个单次样本后半段韵律塌、有"口吃"感。纯 ffmpeg 分析排除"参考停顿被克隆"（参考无内部静音），定位为 **TTS 长输入通病**。**用同文本 + 同参考单次合成复测 3 次**：输出 23.2 / 27.8 / 28.3s（叠加原始 28.5s，**4 样本时长方差 ~19%**），停顿分布从"零长停顿"到"八个 0.5–0.84s 停顿"全谱漂移——**确认 MiMo voiceclone 长输入 run-to-run 不稳定**（质量 + 时长都抖；极端短样本 23.2s 在 29.55s 槽位短 21.5%，会触发对齐 rewrite）。原始口吃只是该高方差分布里的一个样本，**"重生成碰运气"不能作修复**。
- **切短可修（已验证，本期不做）**：seg3 切 4 短句逐句合成再 concat（+trim 接缝）后病态长静音消失、停顿均匀，用户 A/B 判"更顺"——**切片是已验证的压方差/修口吃杠杆**，但见下方产品决策本期不落地。
- **验收门槛核对**：失败率 0%（4/4）✓ ／ p50≈10s < 15s ✓ ／ 质量（保留原声成立，用户判"还行，甚至优于 CosyVoice 克隆"；3 段复测均无口吃，但样本间听感仍有差异）✓。
- **结论：Phase 1 通过，进 Phase 2 规划。** **产品决策（2026-05-29，用户）：免费版本期先不做切片**——(1) 复用的 Express 对齐层（atempo + 补静音 + >20% 才 rewrite）本就吸收 TTS 时长方差，长输入不稳定会优雅降级为"偶尔某段稍慢 / 被重写"、不致任务失败，**无需切片即可上线**；(2) 免费引流漏斗可接受质量波动，付费版（Studio 切分 / 后编辑）是质量升级卖点。切片作为**已验证的 deferred 后备杠杆**归档（用户后续抱怨长段质量再启用），见下表 gate #7 注。

### Phase 2：公开免费版（Phase 1 通过后，且必须先补齐以下 gate）

> 📋 实现计划：[2026-05-29-mimo-free-tier-phase2-plan.md](2026-05-29-mimo-free-tier-phase2-plan.md)（Phase 2a 核心流程 behind flag + Phase 2b 变现；本期不做切片，consent/法务为 LAUNCH GATE）。
以下每条都是 CodeX 核实的**落地必改点**，缺一即出 bug 或泄露：

| # | 落地点 | 现状（已核实） | Phase 2 必做 |
|---|---|---|---|
| 1 | 入口白名单 | `job_intercept.py:1042` 仅认 express/studio/smart，未知模式**静默改 express** | 加 `"free"` 到白名单 + 前端 `TranslationForm.tsx` 类型 + 任务列表 |
| 2 | 计费真源 | `credits_service.py:154` 未知 `(mode,tier)` 落 `DEFAULT_DEBIT_RATE`（非 0） | `pricing_runtime` 加 `(free, standard)=0` 真源，**不能只在 policy 写 credits=0** |
| 3 | 日配额 | `quota.py:39` `free_jobs_quota_total` 是免费**套餐**总额，非按日/按模式 | **独立** `free_service_daily_usage` 表/ledger，勿混用现有额度 |
| 4 | MiMo provider | `mimo_tts_provider.py:48` 硬编码 `audio.voice=mimo_default`；`tts_generator.py:487` 不传 model/参考音频 | 新 voiceclone 分支：model 透传 + base64 音频构造 + 10MB 校验 + mock 测试 |
| 5 | 下载 gate | `downloadable_keys.py:94` 仅 express 受限，其余（含 free）**默认 Studio 全放行** | 加显式 `free` 分支（只放水印成品，草稿/后编辑产物门控）——**不能只靠 UI 隐藏** |
| 6 | fallback 可见性 | — | kill-switch 降级 / MiMo 失败回落，需对用户/admin 可见提示，不静默 |

> **Gate #7「长段切短再合成」— deferred（本期不做，2026-05-29 用户决策）。** Phase 1 复测确认 MiMo voiceclone 长输入 run-to-run 不稳定（同一 126 字文本 + 同参考，单次合成 4 样本时长 23.2–28.5s、~19% 方差，停顿分布大幅漂移），切短是**已验证**的压方差 / 修口吃杠杆。但现有 Express 对齐层吸收时长方差、免费版可接受质量波动，故**本期不落地切片**；归档为后备杠杆，仅当 Phase 2 用户抱怨长段质量时启用（切短句逐句合成 → concat trim 接缝，落 §2.2 step3 / gate #4，阈值 admin 可配）。

## 2. 第 1 段：架构 + 模式分发 + 音色数据流 ✅（已确认）

### 2.1 模式分发
- 新增 `service_mode="free"`，在 `gateway/job_intercept.py::compute_job_policy` 加分支（与 smart/express 并列）返回免费版策略：
  - `wait_for_review=False`（非交互，复用 Express 编排）
  - `tts_provider="mimo"`、`tts_model="mimo-v2.5-tts-voiceclone"`、`voice_strategy="free_voiceclone"`
  - `voice_clone_enabled=False`（**不走** MiniMax/CosyVoice 的 `create_voice→voice_id` 克隆路径——MiMo 无 voice_id）
  - `credits=0`（对用户免费）
- 管线编排**完全复用 Express**（ASR → S2 三轮审校 → 翻译 → TTS → DSP 对齐 → 字幕 retiming → 发布/剪映草稿）；唯一管线差异在 **TTS 音色环节**。

### 2.2 音色数据流（核心）— 已按 spec review 修正
1. demucs 已分离干净人声，落 `audio/speech_for_asr.wav`（`services.audio.separator.speech_filename`）。
   ⚠️ **不能复用 S2 Pass3 的 `clips[sid]`**（spec review 发现）：那些片段从**未分离的** `audio/original.wav`（含背景音）切、16kHz 单声道 opus、落在临时 `.review_tmp/` —— TTS 时已不存在、也非克隆级质量。
2. **新增步骤（参考音频提取）**：从 `speech_for_asr.wav` 按说话人时间轴**重新切出每说话人一份"最长干净片段"**作为 MiMo 参考（**per-speaker 不是 per-segment**，避免音色漂移），以 TTS 级保真（如 24kHz wav）**持久化为 job 产物**（如 `audio/voiceclone_ref/{speaker_id}.wav`），供 TTS 阶段取用。这是一个**明确的新管线子步骤**，不是"复用已有片段"。
3. TTS 阶段：`tts_generator` 加 `_generate_one_mimo_voiceclone(segment, cn_text, speaker_reference_clip)` 分支——调 `mimo-v2.5-tts-voiceclone`，把该说话人参考音频**内联**进 `audio.voice="data:audio/wav;base64,..."`（≤10MB）+ 翻译中文文本 → 该说话人音色的中文音频。
   ⚠️ 注意现 `mimo_tts_provider.synthesize()` 把 `audio.voice` **硬编码为预设音色**、`voice_id` 仅作 `<style>` 字符串 —— 所以这是**新增 param/分支**，不是字段替换。
4. 产物音频照常进 DSP-first 对齐 + retiming（**不破坏对齐不变量**）。

### 2.3 与现有克隆架构的隔离
- 免费版**不碰** `VoiceRegistry` / `UserVoice` 库 / shadow credits / MiniMax clone 端点（那套是 voice_id 模型，MiMo 无 voice_id）。
- `mimo_tts_provider` 加 voiceclone 调用能力（基于 Phase 3 已验证的 `modalities`+`audio` 格式，`voice` 字段换成 base64 data URI；≤10MB）。

## 3. 第 1b 段：免费版 LLM 模型管理 ✅（已确认）

- **解析链复用**：`llm_registry.get_prompt_model("free", prompt_key)` 走现有三级解析——`prompt_models["free"][prompt_key]`（admin override）→ `_MODE_DEFAULTS["free"][prompt_key]` → flat `_DEFAULTS` → `gemini`。
- **新增 `_MODE_DEFAULTS["free"]`**：覆盖 `pass1/pass2/pass3/translate/rewrite/probe_translate/content_compliance`，**默认倾向低成本模型**（控免费成本）。具体默认值在最终 spec 定（候选：translate=`deepseek`/`mimo_v25`、pass2=`gemini`、pass1/3=便宜音频模型如 `gemini_31_flash_lite`/`mimo_v25`、content_compliance=`gemini_31_flash_lite`）。
- **Admin UI**：`admin/prompts/page.tsx` 加"免费版" tab（与 studio/express/smart 并列），逐 stage 选模型。
- **后端**：`gateway/admin_settings.py` 接受并存 `prompt_models["free"]`（与现有模式同结构，无新存储格式）。
- **不变量**：pass1/pass3 仍是 `requires_audio` 阶段，下拉只列音频能力模型（复用 `get_available_models_for_prompt`）。

## 4. 第 2 段：freemium 边界 + 付费解锁 ✅（已确认）

### 4.1 成本闸门
- **10 分钟时长卡口**：job 入口探测（ffprobe）拿到时长后、进昂贵阶段（ASR/LLM/TTS）**之前**校验；`mode=free` 且 > 10min → 拒绝 + 升级提示。上传文件上传后探测，URL 下载后探测。先校验再花钱。
- **每用户每天 1 次配额**：PG 存每用户免费用量；免费 job **创建时**校验，今日已用 → 拒绝 + 升级提示。重置语义建议自然日（固定时区，如 Asia/Shanghai），滚动 24h 备选（开放项）。计数时机：成功创建 job 时 +1。
- **免费计费**：`credits=0`，不扣点；但仍进 metering（admin 成本页可见免费版真实成本 ASR+LLM+TTS），终态走单一入口 `mirror_job_terminal_state`（结算 0 点，不漏记）。

### 4.2 水印（仅免费版）
- 落点：**发布阶段**对 `publish.dubbed_video` 做 ffmpeg 文字叠加（drawtext）；付费版不加。
- admin 可配（存 `admin_settings`）：文字内容、位置（9 宫格锚点或 x/y）、字号、透明度（alpha）。

### 4.3 免费版交付物 + 付费解锁
- **免费交付**：加水印的成品视频。
- **后编辑** 和 **剪映草稿下载** 作为**付费 add-on**（用户先免费看到成品 → 解锁阻力低，是 freemium 变现钩子）。
- 关键：两个 add-on 的平台**边际成本≈0**（草稿是纯门控；后编辑 re-TTS 用 MiMo voiceclone，现免费）→ **价值定价**，非成本定价。

| 解锁项 | 计费 | 默认 | 说明 |
|---|---|---|---|
| **后编辑** | **按时长** | **10 点/分钟，最低 20 点** | 比完整工作台版 15/分低（重管线已免费跑过，后编辑是增量）；10min=100点、3min=30点；底价防短视频白送 |
| **剪映草稿下载** | flat | **50 点** | 一次性产物解锁（≈¥1.5） |
| 打包（可选） | — | 后编辑(时长价) + 草稿，小折扣 | 促连带转化 |

- **全部价格 + 费率进 `pricing_runtime` 做 admin 可配**（与现有每分钟费率同机制），上为默认值。
- 后编辑 re-TTS 计费：MiMo 免费期内"进入即解锁、编辑内 re-TTS 不再逐次扣点"（最简单、体验最好）；MiMo 转收费后改"编辑内 re-TTS 按段计"，admin 可调。
- 锚点：1 点≈¥0.03，`credits=max(1, ceil(cost_rmb/0.03))`；工作台标准 15 点/分。

## 5. 第 3 段：错误处理 / kill-switch / consent / 不变量 / 测试 ✅（已确认）

### 5.1 MiMo 合成失败（best-effort，不破坏任务）
- 单段 voiceclone 失败 → provider 内置重试 → 仍失败则**回落到基础 `mimo-v2.5-tts` 预设音色**（免费）合成该段，任务继续。
- **绝不**在失败路径自动调付费克隆（MiniMax）——付费 API 硬约束。

### 5.2 kill-switch（MiMo 转收费）
- admin 开关 `free_tier_voiceclone_enabled`（默认开）。MiMo 转收费时管理员关 → 免费版 `voice_strategy` 降级到**最便宜预设引擎（CosyVoice）**，免费版继续（丢"保留原声"卖点，平台吃被 10min/1天封顶的小额成本）。
- `compute_job_policy` 的 free 分支读该 flag 决定音色引擎。

### 5.3 consent / 法律（⚠️ 最大非技术风险，必须产品/法务拍板）
- 免费版克隆的是**视频原说话人**（通常非用户本人，是被翻译视频里的第三方）。公开免费 tier 克隆任意视频人声，有**肖像/声音权 + 平台责任**风险。
- **法律基础（CodeX review 补充）**：《民法典》**第 1023 条**明确"对自然人声音的保护，参照适用肖像权保护的有关规定"。MiMo voiceclone 官方定义即"音频样本复刻任意声音"且每请求内联参考音频——**即使用户勾了 ToS，也不等于被克隆说话人本人授权**。
- 建议：上传前 **consent 勾选 / ToS**（"拥有该内容使用权 / 知悉并同意语音合成"）+ admin 可关停 + **公开前先 allowlist/beta，不建议直接全量**（Phase 1 灰度天然满足）。**本项为上线 gate，工程不能单方决定。**

### 5.4 管线不变量（不破坏）
- DSP-first 对齐、字幕 retiming（确定性）、TTS 单元=SemanticBlock、剪映草稿（付费门控）——免费版全部沿用。

### 5.5 付费 API 硬约束合规
- MiMo voiceclone = 用户显式选免费版 + 上传 → 知情的 TTS 步骤（现免费）；无自动付费克隆；失败回落免费预设；credits=0。✅ 合规。

### 5.6 测试清单
- gateway `compute_job_policy` `free` 分支策略正确
- 10min 卡口拒绝 / 1天1次配额 gate / credits=0 结算
- 水印：免费版加、付费版不加
- MiMo voiceclone 内联参考调用（mock urlopen）
- kill-switch 降级 CosyVoice
- add-on 计费：后编辑按时长（10/分，底价20）、草稿 flat 50
- 付费 API 守卫：free 路径无自动 MiniMax 克隆（AST 扫，类似现有 `test_phase1_guards`）

## 6. 开放项 / 待决
- **consent/法律（最高优先）**：克隆视频原说话人音色的授权/合规边界 + ToS 文案 + 落点（产品/法务）
- 免费版 `_MODE_DEFAULTS["free"]` 各 stage 的确切默认模型（倾向低成本）
- 水印实现细节（ffmpeg drawtext vs 图片叠加；admin 配置 schema；剪映草稿是否也需水印/或干脆不给免费用户）
- 每日配额的重置语义（自然日固定时区 / 滚动 24h）、PG 存储模型
- 后编辑 add-on：MiMo 免费期"进入即解锁不逐次扣"，转收费后"按段计"的切换机制
- 每说话人参考片段的"最长干净片段"选取算法（时长阈值、质量门槛）
- **长段切短再合成 — deferred 后备杠杆**（Phase 1 已验证可压方差 / 修口吃，但本期不做，见 §1.5 / gate #7 注）：若将来启用，需定切分阈值（字数 / 时长）、切句策略（标点优先）、接缝 trim/crossfade 参数
- add-on 价格/费率在 `pricing_runtime` 的 schema 落点
