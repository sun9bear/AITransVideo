# UI-06 part2 · 核心工作台英文化（薄切片方案，2026-06-30 起草）

> **状态：☐ 方案待项目主拍板（切片序 + 法务口径）**。这是 [UI-06](UI-06-app-user-flows.md) 的
> part2——把英文「一路做到核心工作台」（母方案 [§9.2 Q2=是](../2026-06-25-ui-page-locale-switch-plan.md)，
> 备海外）。part1（常驻账户页 332 key）已合（[PR #85](https://github.com/sun9bear/AITransVideo/pull/85)）；
> 错误显示底座 [UI-09](UI-09-client-error-layer.md) 已合（[PR #86](https://github.com/sun9bear/AITransVideo/pull/86)）。
> 本文把 part2 拆成可独立 ship 的**薄切片**，并把**法务敏感面（民法典 1023 consent / 克隆授权）单独隔离**。

## 0. 为什么要拆片（不能一刀切）

工作台 CJK 体量 = **625 occurrences / 21 文件**（`scripts/cjk-baseline.json` 实测，已排除
`admin/**` 与 `app/.../workspace/[jobId]/edit/**`——后者被 cjk-guard `EXCLUDE_RE` 永久排除，
**不在 part2 射程**）。一刀切违反 ship-unit「薄、单一目的、可独立评审」纪律，且会把**法务文案**
和**普通 chrome** 混在一个 PR 里（法务面必须人审/counsel，普通 chrome 不必，混在一起会卡死）。

### 现状盘点（按用户旅程分组，数字＝CJK occurrences）

| 旅程阶段 | 文件 | CJK | 法务敏感 |
|---|---|---|---|
| **入口/提交** | `components/workspace/TranslationForm.tsx` | 124 | ⚠️ 含 1023 consent（L78/163-173/590/973-1004）+ 克隆 opt-in + 扣费门 |
| | `components/workspace/NewTranslationDialog.tsx` | 2 | — |
| | `app/[locale]/(app)/translations/new/page.tsx` | 1 | — |
| **任务详情/进度** | `app/[locale]/(app)/workspace/[jobId]/page.tsx` | 50 | — |
| **结果/预览/交付** | `components/workspace/ResultMediaCard.tsx` | 42 | — |
| | `components/workspace/SmartPreviewConfirmDialog.tsx` | 21 | — |
| | `components/workspace/JianyingDraftPathDialog.tsx` | 17 | — |
| | `components/workspace/SmartPreviewResultCard.tsx` | 13 | — |
| **交互审校（Studio）** | `components/workspace/VoiceSelectionPanel.tsx` | 60 | — |
| | `components/workspace/SmartAutoDecisionPanel.tsx` | 39 | — |
| | `components/workspace/TranslationReviewPanel.tsx` | 36 | — |
| | `components/workspace/VoiceReviewPanel.tsx` | 15 | — |
| | `components/workspace/SpeakerAudioAuditModal.tsx` | 12 | — |
| | `components/workspace/EditPageSpeakerCreateDialog.tsx` | 9 | — |
| | `components/workspace/EditPageSpeakerProfileBadge.tsx` | 5 | — |
| **语音克隆面** | `components/voice-clone/CosyVoiceCloneModal.tsx` | 42 | ⚠️ 克隆授权 |
| | `components/workspace/VoiceCloneModal.tsx` | 31 | ⚠️ 克隆授权 |
| | `components/voice-clone/CosyVoiceSegmentPicker.tsx` | 22 | — |
| | `components/voice-clone/CosyVoiceConsentModal.tsx` | 13 | ⚠️ 克隆 consent 文案 |
| **后编辑（post-edit）** | `components/workspace/edit/SegmentRow.tsx` | 53 | （AVT_ENABLE_POST_EDIT gated） |
| | `components/workspace/edit/CurrentSegmentOpsPanel.tsx` | 18 | （同上） |

> `app/.../workspace/[jobId]/edit/*`（编辑页主体）被 cjk-guard 排除 → **整支 post-edit 不译**
> （out-of-scope，非「待做」）。`components/workspace/edit/*`（共 ~85 CJK）虽未被排除，但只在
> AVT_ENABLE_POST_EDIT 渲染、非海外 headline 漏斗 → 排最后或一并 out-of-scope（见 §6 决策点）。

## 1. 切片划分与推荐序

| 切片 | 内容 | CJK | 法务 | 价值 |
|---|---|---|---|---|
| **W1 任务详情→结果/下载** | `workspace/[jobId]/page` + ResultMediaCard + SmartPreviewResultCard + SmartPreviewConfirmDialog + JianyingDraftPathDialog | ~143 | **干净** | 「看到并下载我的成片（英文）」——UI-09 错误层在此兑现 |
| **W2a 上传/提交表单 chrome** | TranslationForm（**非 consent 部分**：模式选择/字段/按钮/扣费门 toast）+ NewTranslationDialog + translations/new | ~100 | 半（consent 隔离到 W2b） | 漏斗**入口**，en 用户才能开任务 |
| **W2b 1023 consent / 免费档授权文案** | TranslationForm consent blocks（L78/163-173/590/973-1004） | ~25 | **HARD（counsel）** | 法务文案，须 owner+counsel 签（类比 UI-03c） |
| **W3 交互审校（Studio）** | VoiceSelectionPanel/SmartAutoDecisionPanel/TranslationReviewPanel/VoiceReviewPanel/SpeakerAudioAuditModal/EditPageSpeaker* | ~176 | — | Studio 复核步骤，体量大 |
| **W4 语音克隆面** | CosyVoiceCloneModal/VoiceCloneModal/CosyVoiceSegmentPicker/CosyVoiceConsentModal | ~108 | **HARD（克隆授权 counsel）** | 克隆 UI，consent 文案须 counsel |
| **W5 post-edit** | components/workspace/edit/* | ~85 | — | gated 高级功能；建议 out-of-scope（见 §6） |

### 推荐序（绕开法务阻塞先交付价值）

**W1 先行**（推荐）：法务干净、能立刻 ship、正好兑现刚合并的 UI-09 错误层、交付「拿到成片」时刻。
→ 然后 **W2a**（漏斗入口 chrome，consent 隔离/gated 不动）→ **W2b**（待 counsel 签 en consent 后）
→ **W3**（审校）→ **W4**（克隆，待 counsel）→ **W5**（post-edit，或 out-of-scope）。

> 备选：若 owner 认为「漏斗入口」优先级高于「拿结果」，可 **W2a 先行**（en 用户先能开任务），
> W1 紧随。两者都不碰 W2b/W4 的法务文案。**入口（W2a）vs 出口（W1）谁先 = owner 决策点（§6 Q1）。**

## 2. 法务敏感面处理（硬约束）

- **W2b / W4 的 consent / 克隆授权文案 = HARD 人审单元**：与已签的 [UI-03c](UI-03c-legal-pages.md)
  同级。**Claude 先做忠实翻译第一遍 + 标疑点，counsel/owner 终签**。疑点同 UI-03c：中文锚《民法典》
  1023 +「由我自行承担」责任转移；海外应改 US right-of-publicity / EU GDPR voice-as-biometric
  市场化表述——译文是**忠实翻译非市场化法务文本**。
- **隔离手法**：W2a/W3 等普通 chrome 切片**不得**夹带 consent 文案；consent 字符串在 W2b/W4 才迁，
  且迁法可选 **(a) bilingual zh-anchored**（UI-03c 先例，对 legal 有意 R1 豁免）或 **(b) 保持 consent/
  克隆 UI 在现有 flag（`NEXT_PUBLIC_ENABLE_FREE_TIER` 等）后**，公共 en 漏斗默认不渲染 → 不阻塞 W2a。
- **付费 API 硬约束不变**：clone/consent 入口在 counsel 口径 + flag 双 gate 前保持关闭；纯本地化**不**
  改任何 clone 触发条件（CLAUDE.md「付费 API 不能自动调用」+「CosyVoice 免费克隆澄清边界」原样）。

## 3. UI-09 错误层接入（每片随带）

每个工作台切片**顺带**把本片文件里的错误显示路由到已合的 `localizeError`（[UI-09](UI-09-client-error-layer.md)）：
- 27 个 `getErrorMessage(err)` 点 + 5 处硬编码中文前缀（`workspace/[jobId]/edit/page` 的「重试失败/保存
  失败/改说话人失败/拆分失败」+ TranslationReviewPanel「拆分失败」）→ `localizeError(err)`（前缀改 ICU key）。
  *注：edit/page 的 4 处在 post-edit 子树（W5/out-of-scope），随 W5 处理或不处理。*
- 按本片真实 call site 命中的 job-create 码补 `errors.code.*`（UI-09 故意留空的部分）：
  **静态 message 码**（`free_disabled`/`consent_required`/`smart_disabled`/`convert_already_exists`/
  `upload_not_found`/`invalid_source` 等）→ zh **verbatim 照搬后端**（红线 R1）+ en；**动态 message 码**
  （`insufficient_credits`/`duration_*`/`quota_exhausted`/`concurrent_limit` 等带 `{...}` 占位）→
  **不进 code.\***（passthrough，诚实漏中文，待 UI-BE-01 发结构化 params）。具体码清单见 UI-09 Discovery
  workflow 结果（40 码，`gateway/job_intercept.py`）。
- W2a 是 code.* 主消费方（扣费门/consent gate 都在 job-create）：补静态码即可让 en 用户在创建失败时见英文。

## 4. 红线 / 不变量（沿用 part1 + UI-09）

- **R1 默认 zh 字节一致**：迁出的内联中文 → message key，zh 值**逐字节照搬**；zh-snapshot 加本片代表串
  pin；cjk-baseline 只减不增（migration 完成的串从 baseline 移除）。
- **R5 content 透传**：job/项目标题、`display_title_zh`、说话人名、转录/译文、voice 名、视频 id、demo
  名**不译**（passthrough）。审校面（W3）尤其多 content，逐一甄别。
- **admin 子树 operator-only 不译**；**pipeline 语言字段不碰**（与 [target_language 配音轴](2026-06-14...) 正交）。
- **付费 API 硬约束**：纯表现层不碰 clone 触发；W2b/W4 consent gate 在 counsel 前不放开。
- **next-intl typed-key**：动态 key 走 `t.has()` 守门 + `Parameters<Translator>[0]` cast（part1/UI-09 同款）。
- **新串 message key、不得新写内联 CJK**（cjk-guard 强制）。

## 5. Gate / 守卫（每片 ship 前）

`tsc 0 / eslint 0 / next build 0` + 5 个 uiloc 守卫全绿：
- `uiloc:zh-snapshot`（加本片 zh 代表串 R1 pin）
- `uiloc:key-parity`（新 namespace 自动纳入）
- `uiloc:cjk-guard`（本片迁完后 baseline 只减；新增内联 CJK = red）
- `uiloc:intl-guard` / `uiloc:hreflang-check`（不回归）

外审：多 lens 对抗评审（R1 字节一致 / R5 passthrough / 法务隔离 / UI-09 错误接入正确 / typed-key 存在性）
→ CodeX CLI → push → @codex bot 终审 → required CI 全绿（backend-full-suite 非必需）。

## 6. DoD + 诚实缺口

- **DoD**：本片所辖文件 `/en` 渲染全英文（content 透传除外）；zh 字节一致；错误显示路由 localizeError；
  法务文案**未在本片改**（除非是 W2b/W4 且 counsel 已签）。
- **诚实缺口（必标）**：① 未编码后端错误在 en 仍漏中文（UI-BE-01，沿用 UI-09）；② 未迁切片的工作台文件
  在 en 仍中文——**不得**称「核心工作台已全英文」，须列已迁/未迁切片清单；③ post-edit 主体（`[jobId]/edit/*`）
  按 cjk-guard 排除**永不译**，须显式声明非缺口而是边界。

## 7. 待项目主拍板（执行前）

- **Q1 切片序**：W1（拿结果，法务干净）先，还是 W2a（漏斗入口）先？
- **Q2 法务口径**：W2b/W4 consent en 文案——(a) 等 counsel 签市场化 en，还是 (b) 先 bilingual zh-anchored
  上线（UI-03c 先例）？在 counsel 给口径前，consent/克隆 UI 是否保持 flag 关闭即可？
- **Q3 W5 post-edit**：`components/workspace/edit/*`（~85 CJK，gated）译还是判 out-of-scope？
- **Q4 W3 审校切片粒度**：176 CJK / 6 文件，是否再拆（如 VoiceSelectionPanel 单独一片）？
- **Q5 部署**：各片合 main 后，prod 暴露 /en 工作台给登录用户仍需 owner 部署（Via-154）——同 part1 部署门。

## 8. 给执行 context 的交接

- **底座已就位**：UI-09 `useApiErrorMessage()`/`localizeApiError`（`lib/api/error-localization.ts`）+
  `errors` namespace；part1 的 6 个 app* namespace + translator 首参线程化模式可直接扩展。
- **worktree**：`D:/Claude/avt-worktrees/uiloc-intl-formatters`（node_modules 在、可复用）——从 `origin/main`
  另起分支（如 `uiloc/workbench-w1`）。⚠️ D: 盘紧张，注意磁盘。
- **先做 anchor**：读本文 + 母方案 §9.2 + UI-09 + UI-06 part1 + UI-03c（法务先例）；sub-agent 不继承项目
  指令，须让其先读 anchor。
- **每片 ship 走 ship-unit**：anchor → branch → test-first/实现 → 本地 gate → 多 lens 对抗 → CodeX → PR →
  @codex → 收敛 → squash-merge（**里程碑边界/法务面须 owner 签**）→ 同步 INDEX/LOG。
