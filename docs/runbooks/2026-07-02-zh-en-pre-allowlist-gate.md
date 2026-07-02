# zh→en 上线前人评门操作手册（CM-03）

- 状态：**材料包完成，跑批与人评待项目主显式触发**（付费 API 硬约束——脚本不自动跑）。
- 关联方案：[`docs/plans/2026-07-02-commercialization-sprint-plan.md`](../plans/2026-07-02-commercialization-sprint-plan.md) §2 CM-03、[`docs/plans/2026-06-13-multilingual-mutual-translation-plan-v3.md`](../plans/2026-06-13-multilingual-mutual-translation-plan-v3.md) Phase 0 第 4 点 + §5 第 5 条（pre-allowlist 人评门定义）。
- 执行脚本：[`scripts/calibrate_zh_en_ratio.py`](../../scripts/calibrate_zh_en_ratio.py)（默认离线，见 §1）。
- 本文档只负责**操作步骤**；不含任何自动触发逻辑——所有涉费用步骤都要 owner 手动敲命令。

---

## §1 校准跑批操作步骤

### 1.1 准备语料（两种途径任选其一，或混用）

**途径 a：真实 ASR transcript（推荐，最贴近生产）**

1. 找 2-3 段中文短访谈/口播视频（建议每段 1-3 分钟，覆盖不同语速/说话人）。
2. 用现有管线单独跑到 S1（转录）阶段即可，不需要跑完整个 job；或者直接复用某个
   已完成 job 的 `transcript/transcript.json`（真实产物，`lines[]` 每项含
   `source_text`）。
3. 把这些 `transcript.json` 文件拷到一个语料目录，比如：
   ```
   data/cm03_corpus/
     clip1_transcript.json
     clip2_transcript.json
     clip3_transcript.json
   ```
   脚本会把 `segments`/`lines` 数组里每个对象的 `text` / `source_text` /
   `cn_text` 字段当作一条口述行读取（三个 key 任一命中即可，兼容真实
   transcript.json 与 segments.json 两种产物形状）。

**途径 b：纯文本段落（最快，适合先跑一版粗估）**

直接写 `.txt` 文件，每行一条口述句（空行会被跳过）：

```
data/cm03_corpus/
  clip1.txt
  clip2.txt
  clip3.txt
```

范例内容（`clip1.txt`）：

```
今天天气不错，我打算去公园散散步，顺便买点水果回来。
你觉得这个周末我们一起去爬山怎么样？
```

两种格式可以在同一个语料目录里混用；脚本会把目录下每个 `.json`/`.txt`
文件当作**一个 clip**。建议 N=3-5 个 clip，与 §2 rubric 的人评样本量对齐（同一
批语料可以复用：既喂给校准脚本算 ratio，也是人评 rubric 的抽查对象）。

### 1.2 跑离线费用预估（默认行为，随时可跑，零费用）

```bash
python scripts/calibrate_zh_en_ratio.py --corpus data/cm03_corpus
```

`--estimate` 是默认模式（不加任何 `--run`/`--i-approve-paid-llm-calls` 也会跑
这条路径），纯离线：只统计 clip 数/段数/中文字符量，按 `gemini-3.1-pro-preview`
公开定价换算成一张费用预估表，**全程不 import、不实例化 `GeminiTranslator`**，
不产生任何网络请求。示例输出（3 个 clip、7 段、167 个中文字符）：

```
========================================================================
CM-03 zh->en calibration -- OFFLINE cost estimate (no API calls made)
========================================================================
corpus dir       : data/cm03_corpus
clips            : 3
segments (lines) : 7
CJK chars total  : 167
all chars total  : 184
------------------------------------------------------------------------
pricing model    : gemini-3.1-pro-preview (conservative UPPER bound)
pricing source   : public pricing trackers, checked 2026-07-02 ($2.00/M in, $12.00/M out; standard non-batch rate)
actual engine    : decided at --run time by llm_registry routing (mode=studio, task=translate;
                   flat default is deepseek -- far cheaper than the Gemini rate above -- and an
                   admin override in admin_settings.json may apply). The real model is printed
                   by --run and recorded in the report; this table prices the WORST case.
est. input tokens  : ~513
est. output tokens : ~256
est. input cost    : $0.0010
est. output cost   : $0.0031
est. TOTAL cost    : $0.0041
------------------------------------------------------------------------
This is a PURE OFFLINE estimate. No LLM/network call has been made.
To run the real paid calibration against this corpus, the owner must
explicitly pass BOTH switches:

    python scripts/calibrate_zh_en_ratio.py --corpus data/cm03_corpus --run --i-approve-paid-llm-calls

========================================================================
```

费用量级参考：**一个 3-5 clip 的校准语料预计花费在 1 分钱美元以内**（真实 clip
通常比这个 3 句话的样例长，预计仍在几美分到几毛美元量级——真实数字以每次
`--estimate` 的实际输出为准，不要凭这个示例外推大语料）。

> **预估表是"最坏情况"报价**：真实引擎由 llm_registry 按生产 Studio 同款路由
> 决定（task=translate 平面默认 `deepseek`=deepseek-v4-flash，约 $0.14/$0.28
> 每百万 token，比表里 Gemini 价便宜一个数量级；admin 在
> `admin_settings.json::prompt_models["studio"]["translate"]` 的覆盖优先）。
> 真实模型以 `--run` 开头打印的 `[route] ...` 行和报告里的
> `effective_translate_route` 字段为准。

### 1.3 Owner 触发真实跑批（唯一涉费用步骤）

**前置条件**：
1. `GEMINI_API_KEY` 已设置（`GeminiTranslator` 构造必需 + registry fallback 链
   里有 Gemini 候选）。
2. **实际路由到的 provider 的 key 也要设置**——默认路由是 `deepseek`
   （`deepseek-v4-flash`），需要 `DEEPSEEK_API_KEY`；若 admin 覆盖了
   `prompt_models["studio"]["translate"]`，按覆盖后的模型对应 env（脚本开跑前
   会自查：路由模型的 key 缺失时直接报错退出，不会跑到一半逐 clip 失败）。
   跟管线生产环境同一批 key 即可（`.env` 里已有的那些）。

```bash
python scripts/calibrate_zh_en_ratio.py --corpus data/cm03_corpus \
    --run --i-approve-paid-llm-calls
```

- **两个开关缺一都会被拦截**（非零退出 + 打印费用警告，不产生任何调用）：
  - 只给 `--run`：打印 `[blocked] --run was passed WITHOUT --i-approve-paid-llm-calls`。
  - 只给 `--i-approve-paid-llm-calls`：打印 `[blocked] ... WITHOUT --run`。
  - `--estimate` 与 `--run` **互斥**：同给（无论是否带 approve）一律 exit 2，
    显式的离线请求绝不落入付费分支。
- 两个开关都给：脚本对语料里每个 clip 调用与管线**同一个** `GeminiTranslator.translate()`
  入口（`services.gemini.translator`），并镜像 process.py 的
  `translator._service_mode = "studio"` 注入——**引擎选择与生产 Studio zh→en
  任务完全一致**（llm_registry 路由：默认 deepseek、admin 覆盖生效、同一条
  fallback 链）。开跑第一行会打印实际生效路由，务必核对：
  ```
  [route] task=translate service_mode=studio -> model=deepseek (api_model_id=deepseek-v4-flash, provider=deepseek); fallbacks=['gemini', 'gemini_31_flash_lite', 'mimo_v25', 'mimo_omni']
  ```
  实测 `target_word_count / source_cjk_chars` 的 ratio 分布，按 clip 分别汇总
  + pooled 汇总。
- 单个 clip 失败（网络错误/API 报错等）会被记录并跳过，不中断整批；**全部 clip
  都失败**才会以非零退出（report 里 `"fatal": true`）。
- 产物：`docs/reports/{timestamp}-cm03-zh-en-ratio-calibration.json` +
  同名 `.md`（`--output-dir` 可覆盖默认 `docs/reports`）。Markdown 报告含
  实际生效路由（`effective_translate_route`）、pooled p10/p25/p50/p75/p90/mean
  + 每 clip 明细 + 结论（`maintain_0.55` / `update_ratio` + 建议值）。

---

## §2 人评 rubric checklist

**样本量**：N=3-5 个 clip（与 §1.1 语料复用；若语料更大，从中随机/代表性抽 3-5
个即可，不需要全量人评）。

**三维打分**——每个 clip 逐项填写，签字人在末尾统一确认：

### 维度① 翻译保真（1-5 分，每 clip 一个分数）

评分基准：英文译文是否准确传达中文原意（信息完整、语气/语域匹配、无明显误译
或漏译）。

| 分数 | 含义 |
|---|---|
| 5 | 完全准确，语气自然，专业术语/人名/品牌名处理得当 |
| 4 | 基本准确，个别措辞可优化但不影响理解 |
| 3 | 大意正确，但有可感知的生硬/欧化中文直译痕迹 |
| 2 | 部分信息丢失或误译，影响对内容的理解 |
| 1 | 严重误译/漏译，译文与原意脱节 |

### 维度② wrong-script 零容忍（每 clip 一个 pass/fail，不打分）

检查范围（三处都要看，对应 v3 方案 §1 [C] 三写出器）：
1. **英文输出**（`DubbingSegment.cn_text`，此处实为英文正文）——不得混入未翻译
   的中文残留片段。
2. **字幕文件**（`subtitles_target.srt` / 目标语言字幕 artifact）——不得出现
   CJK 字符（专有名词音译除外，若确需保留原文标注需在 rubric 备注栏说明）。
3. **剪映草稿**（`jianying_draft` 产物里的字幕轨文本）——同上，不得有 CJK 残留。

判定：**任一 clip 任一处出现 CJK 残留 → 该 clip fail**。这是硬性红线，不是
程度分——wrong-script 意味着某处管线仍在用 zh alias 而非目标语言 artifact
（v3 方案订正过的三处写出器耦合点）。

### 维度③ 单位换算后 duration drift

计算方法：

```
drift = |合成音频时长 - 源片段时长| / 源片段时长
```

- 「合成音频时长」= 该 clip 配音产物的 `actual_duration_ms`（或直接量音频文件
  时长）。
- 「源片段时长」= 该 clip 对应原始中文片段的 `end_ms - start_ms`。
- 建议阈值（标注为**建议值**，见下方签字栏）：**p90 ≤ 15%**（即 3-5 个 clip 中
  至少 90% 分位的 drift 不超过 15%）。
- 已知限制（据实记录，不算 fail）：zh→en 首发**明确关闭了 voice-speed 的
  cps 维度**（v3 方案 Phase 4 第 2 点），时长适配完全依赖 DSP/rewrite；drift
  数字应作为「当前已知限制下的真实水平」记录，而不是拿来倒推去改 cps 逻辑
  （cps 重新校准是 GA 前独立工作项，不在 CM-03 范围）。

### 通过阈值（建议值，需 owner 签字确认，非自动判定）

| 维度 | 建议阈值 |
|---|---|
| ① 翻译保真 | 均分 ≥ 4.0 |
| ② wrong-script | = 0（零容忍，任一 clip fail 即整体不过） |
| ③ duration drift | p90 ≤ 15% |

> 以上阈值是本文档作者（Claude，材料包编写者）依据 v3 方案 rubric 定义草拟的
> **建议起点**，不是已被业务方拍板的硬性数字。Owner 执行人评时可以按实际样本
> 观感调整，但**调整后的最终阈值需在下表签字时写明并确认**，作为本次上线判定
> 的依据存档。

### 签字确认栏

| Clip | ①保真(1-5) | ②wrong-script(pass/fail) | ③drift(%) | 备注 |
|---|---|---|---|---|
| clip1 | | | | |
| clip2 | | | | |
| clip3 | | | | |
| clip4（可选） | | | | |
| clip5（可选） | | | | |

**最终判定阈值（签字时确认或调整）**：
- 翻译保真均分 ≥ ____（建议 4.0）
- wrong-script = ____（建议 0，零容忍）
- duration drift p90 ≤ ____%（建议 15%）

**签字人**：________________
**日期**：________________
**判定结果**：☐ 通过，可推进 §3 生产真金 E2E　☐ 不通过，问题记录：________________

---

## §3 生产真金 E2E 清单

前提：§2 人评门已通过签字。以下是**项目主**在生产环境执行的 step-by-step
（工程侧只提供操作清单，不代为执行——涉真实生产 allowlist 翻改 + 真金 job）。

1. **加 allowlist**：生产 admin 后台（或直接改 `admin_settings.json` /
   走 admin API）把 owner 自己的 `user_id` 加入
   `language_pairs_allowlist`，并确认：
   - `language_pairs_enabled = true`（主开关）
   - `language_pairs_user_allowlist_enabled = true`（保持 allowlist 生效，
     不要顺手把它关掉变成对所有登录用户开放）
   - 可选：`voice_catalog_target_language_filter_enabled = true`
     （若要同时验证音色目录按目标语言过滤，非本次 E2E 的强制前提）。
2. **建 Studio 非交互 zh→en 任务**：用 owner 账号登录前端，创建一个
   Studio job，source_language=`zh-CN`、target_language=`en`。首发裁决是
   **非交互 lane**（`requires_review=False`，方案 v3 §「首发 Studio lane 决策框」），
   前端应看不到 voice_selection_review 暂停界面，任务应自动跑到底。
   建议用 §1.1 准备的同一批语料对应的原始视频/音频作为输入，方便交叉核对
   人评阶段和真金阶段的结果是否一致。
3. **等任务跑完，逐项验收**：
   - 目标字幕（`subtitles_target.srt` 或前端下载的字幕文件）**纯 Latin
     字符**，无 CJK 残留。
   - 若产出双语字幕，源/目标两条轨**逐句成对**（不错位、不缺行）。
   - 剪映草稿下载后打开，字幕轨是**英文**（非中文 alias 复制）。
   - 记录该任务的时长漂移（同 §2③ 计算方法），与人评阶段的样本量级做交叉核对。
   - 检查该任务的 metering/cost 记录里 `language_pair` 维度是否正确标记为
     `zh-CN->en`（确认 v3 方案 [E] 成本子系统语言维度已生效，非本次 CM-03 
     强制验收项，但顺手核对成本上是否可追溯）。
4. **完成后撤 allowlist**：验收完（无论通过与否）都要把 owner 的
   `user_id` 从 `language_pairs_allowlist` 移除，避免这条真金验证账号
   变成事实上的常驻灰度用户。若准备正式扩大灰度，走独立的灰度计划
   （不是把验证账号直接留在 allowlist 里顺延）。

---

## §4 Phase B 收尾说明（跑批结论如何落地）

本 PR（CM-03）**不改任何生产代码**——`natural_length_ratio=0.55` 的更新/维持
决策是**下一步 Phase B**（人评通过、跑批结论确定之后）的独立改动，理由：
1. 铁律要求本单元不碰生产代码；
2. ratio 更新影响两个下游消费点（见下），改之前应该先有人评通过的信号，而不是
   校准脚本一出数字就自动改代码。

**Phase B 需要做的事**（跑批 + 人评都通过之后）：

1. **落地 ratio 值**：
   - 若脚本结论是 `maintain_0.55`（pooled p50 与 0.55 偏差 ≤10%）：
     `src/services/language_registry.py:271` 的
     `natural_length_ratio=0.55,  # provisional; re-measure in Phase 0 (plan §3.4)`
     这一行的注释从「provisional」改为标注**已实测确认维持**（例如改成
     `# measured 0.55 confirmed by fixture calibration YYYY-MM-DD, see docs/reports/...`），
     数值本身不变。
   - 若结论是 `update_ratio`（偏差 >10%）：把 `271` 行的浮点值改成脚本报告里
     给出的建议值（两位小数），注释同步指向本次校准报告文件名。
   - 无论哪种，**都要把 `RATIO_CALIBRATION_PENDING` 保持为空 frozenset**——
     它本来就不是「待办占位」，而是「至今没有一个 `pipeline_ready=True` 的
     pair 使用未实测 ratio」这个不变量的钉子（`language_registry.py:280-285`
     的类型注释）。zh-CN→en 在 CM-03 之前就已经 `pipeline_ready=True`
     （允许 allowlist canary 执行），0.55 从「未实测的估计值」变成「已用
     fixture 实测校准过的值」之后，这个 frozenset 依然应该是空的——因为它
     的语义是「有 pipeline_ready pair 在用没实测过的 ratio」，而不是
     「有 pair 曾经用过 provisional 值」。
2. **对齐回归测试**：`tests/test_language_registry.py:249` 的
   `test_zh_en_ratio_is_provisional_0_55`：
   - 若维持 0.55：测试名字本身带有 "provisional" 字样，届时可以考虑重命名为
     `test_zh_en_ratio_is_calibrated_0_55`（或类似），断言值不变，只是语义
     从「这是个占位估计」变成「这是个实测确认值」。
   - 若更新数值：断言里的 `0.55` 连同测试名一起改成新值对应的语义。
   - 无论哪种，`test_zh_en_ratio_is_not_calibration_pending_for_canary`
     （:260-261）和 `test_no_pipeline_ready_pair_has_an_uncalibrated_ratio`
     两个测试不需要动——它们验证的是 `RATIO_CALIBRATION_PENDING` 空集不变量，
     跟 ratio 具体数值无关。
3. **两个下游消费点回归验证**（Phase B 改完 ratio 后建议跑一次，非 CM-03 范围但
   记录在此供 Phase B 执行人参考）：
   - `services/gemini/translator.py` 的 `_estimate_dynamic_target_chars`
     （5 处调用点）+ `_count_cn_chars` 重试 gate——长度预算直接吃这个 ratio。
   - `src/pipeline/process.py` 的 voice-speed cps 元数据
     （`target_chars_per_second`）——**当前处于停用状态**（v3 方案 Phase 4
     第 2 点，zh→en 首发明确关闭 speed 维度），所以这个消费点改 ratio 目前
     不会产生可观测行为变化，只有等 cps 重新校准（GA 前独立工作项）后才会
     真正生效。

---

## 附：CM-03 铁律回顾（供执行人自查）

- 校准脚本默认绝不联网；真跑必须 `--run` + `--i-approve-paid-llm-calls` 同时
  给出，否则打印费用预估并非零退出。
- 本 runbook 对应的 PR **不改任何生产代码**——ratio 常量更新是 Phase B。
- commit 用显式 pathspec；不 push；commit message 不带 AI 署名 trailer。
