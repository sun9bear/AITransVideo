# 界面语言切换（UI Page Locale）· 任务单元总索引（UILOC-00）

> 母方案：[`../2026-06-25-ui-page-locale-switch-plan.md`](../2026-06-25-ui-page-locale-switch-plan.md)（canonical，v3）。
> 本目录把母方案拆成可独立派发、可独立成 PR 的任务单元，供 **ship-unit** 单元循环逐个收口。
> **本阶段只产出文档、不执行代码。**
>
> **轴区分（最重要）**：本系列是 **UI/界面语言**（chrome 中⇄英），与产品 **`target_language`/配音方向**（`docs/plans/2026-04-15-i18n-target-language-direction.md` + `2026-06-13-multilingual-mutual-translation-plan-v3.md`）是**两条正交轴**，勿混（母方案 §0.1）。
>
> **状态（2026-06-25）**：M1 四单元（UI-01..04）已 flesh 到可执行；Phase 2/3 单元（UI-05..09）+ 后端轨（UI-BE-01）为**枚举占位/指针**，待项目主在母方案 §9.2 决策后细化。

## 如何使用（配合 ship-unit）

1. 按 **Wave 顺序**推进；同 Wave 内「可并行=是」的单元可分给不同 agent，各自**独立 worktree + feature 分支**（遵守 [CLAUDE.md 多 agent git 协作模型](../../../CLAUDE.md)：禁止多 agent 共用工作树做改状态操作）。
2. 认领单元 → 读该单元文档（先读母方案对应 §）→ 建 `uiloc/...` 分支 → 按 Step 执行（每步先过该步验收）→ 全单元 DoD 达标 → 多 lens + CodeX CLI + `@codex` bot + CI 绿 → 项目主 review → squash-merge → 勾掉本索引状态 + 回填实施 LOG。
3. 每个单元文档 **Step 0 都是「确认现状」**：执行时先复核 `file:line`（多 agent 并行，行号会漂移），以实际代码为准。
4. **命令环境**：默认 **Git Bash / CI Linux**；PowerShell 执行者改等价命令（`grep`→`Select-String`、`test -f`→`Test-Path`、`tail`→`Select-Object -Last`）。
5. **里程碑边界=人工门**：ship-unit 跑完 **M1**（UI-01..04）后**停在边界报项目主验收**，不自主跨入 Phase 2（Wave U-B 受 §9.2 决策门约束）。

## 状态图例

`☐ 待开始` ｜ `◐ 进行中` ｜ `✅ 已完成` ｜ `⏸ 阻塞/决策门`

## 任务单元清单

### Wave U-A — M1（基础设施 + 营销 + 最小 Auth；**已 flesh，可派发**）

| 单元 | 文档 | 状态 | 母方案 Phase | 工时 | 可并行 | 建议分支 |
|---|---|---|---|---|---|---|
| UI-01 | [i18n 基础设施](UI-01-i18n-foundation.md) | ✅ | P0a | M | 否（先行） | [PR #46](https://github.com/sun9bear/AITransVideo/pull/46) 已合并 |
| UI-02 | [路由迁移 + proxy + 切换器](UI-02-locale-routing-migration.md) | ✅ | P0b | L | 否（依 UI-01，spike 先行） | [PR #48](https://github.com/sun9bear/AITransVideo/pull/48) 已合并（squash `56404a55`） |
| UI-03a | [营销·结构化文案抽取](UI-03-marketing-en-seo.md)（§子单元拆分） | ✅ | Phase 1·T1.1 | M | 是（依 UI-02） | [PR #50](https://github.com/sun9bear/AITransVideo/pull/50) 已合并（squash `f30c1506`） |
| UI-03b | [营销·内联重文案 hero/pricing/trial](UI-03-marketing-en-seo.md) | ✅ | Phase 1·T1.2 | M | 是（依 UI-03a） | [PR #53](https://github.com/sun9bear/AITransVideo/pull/53) 已合并（squash `c26bd43e`）。含**收尾**：补完三页渲染的 6 leak 组件（trial-details/primary-cta/plan-card-cta/anonymous-trial-launcher【占位分支】/**hero-sample-player/seal-stamp** 后两者原卡又漏列、核渲染链揪出）。AnonymousTrialPanel（flag-ON consent 漏斗）延后独立单元 |
| UI-03c | [营销·legal 人审](UI-03c-legal-human-review.md)（HARD 人审） | ◐ **第一轮人审完成：拒签** | Phase 1·T1.2 | M | 是（**并行，不阻塞 3a/b/d**） | `uiloc/marketing-en-seo-c`；项目主第一轮人审记录 [UI-03c-legal-human-review.md](UI-03c-legal-human-review.md)：**3 hard blocker**（terms 7.2 自动续费↔实际不续费；privacy 百度网盘断开/披露↔代码软断开）→ **先修中文源再做英文**。决策：**百度网盘从条款删除**（见范围修正 §） |
| UI-03d | [营销·SEO 翻旗 + EN 排版](UI-03-marketing-en-seo.md)（拆 d1/d2） | ✅ | Phase 1·T1.3 | M | 否（依 UI-03a+b+e/f 合并） | **d1 SEO 翻旗** [PR #63](https://github.com/sun9bear/AITransVideo/pull/63)（squash `1e93d6fc`）+ **d2 EN 排版** [PR #64](https://github.com/sun9bear/AITransVideo/pull/64)（squash `d3036b83`）。home/pricing/trial 互惠 hreflang + en metadata；legal 页 en hreflang 留 UI-03c |
| **UI-03e** | **首页区块英文化（上半）**：PainPoints/FeaturedDemos/ProductProof/WorkflowShowcase | ✅ | Phase 1·T1.2' | M | 是（依 UI-03a） | [PR #60](https://github.com/sun9bear/AITransVideo/pull/60) 已合并（squash `8ddf8690`） |
| **UI-03f** | **首页区块英文化（下半）**：Features/SuitedScenarios/TrustBanner/PricingPreview/FinalCta（link-button 无 CJK 不动）。⚠️ **primary-cta/plan-card-cta/anonymous-trial-launcher 已由 UI-03b 字典化、03f 只消费不再迁** | ✅ | Phase 1·T1.2' | M | 是（依 UI-03a/b，与 03e 串行 cjk-baseline） | [PR #61](https://github.com/sun9bear/AITransVideo/pull/61) 已合并（squash `d94f2099`） |
| UI-04 | [最小 Auth 英文化](UI-04-min-auth-en.md) | ✅ | Phase 1.5 | M | 是（依 UI-02） | [PR #49](https://github.com/sun9bear/AITransVideo/pull/49) 已合并（squash `85f2e7c3`） |

### ⚠️ 范围修正（2026-06-26 CodeX 审核 + 项目主决策）

**起因**：CodeX 审核指出 UI-03b「实现完成」是高估；核查发现**原 UI-03 方案卡的组件清单漏列了首页 ~9 个区块组件 + 几个 CTA + trial-details**。`/en` 首页实际渲染链 = Hero(英)→PainPoints(中)→FeaturedDemos(中)→ProductProof(中)→WorkflowShowcase(中)→Features(中)→SuitedScenarios(中)→ToolComparison(英)→TrustBanner(中)→PricingPreview(中)→Faq(英)→FinalCta(中)。**UI-03a+b 只英文化了 ~40% 营销表面**；`/en` 首页主体仍中文。

**项目主决策（AskUserQuestion 2026-06-26）**：
1. **范围 = 补全整个 /en 营销**（Q1）→ 新增 **UI-03e/UI-03f**（首页区块 + CTA 全英文化）；**UI-03b 必须先补完页内泄漏组件**（TrialDetails/CTA，否则其 hero/pricing/trial 页半中半英）再 PR。M1 体量随之增大（marketing 工作量约翻倍）。
2. **百度网盘从条款删除**（Q2）→ UI-03c legal 中文源：privacy 删除百度网盘专条；但**因管理员归档确会把用户上传/生成内容外流到运营方百度网盘（真实数据流），删除时须以「运营方可能使用第三方云存储归档已完成任务材料」的通用措辞兜底，避免少披露**（实施者注意，非纯删）。

**M1 真实剩余工作**：~~UI-03b 补完~~（✅ #53）→ ~~UI-03e + UI-03f（首页区块）~~（✅ #60 / #61）→ ~~UI-03d（SEO 翻旗 #63 + EN 排版 #64）~~（✅，home/pricing/trial 已挂互惠 hreflang + en metadata + EN 衬线）→ **仅剩 UI-03c legal（owner 人审门，不自动合）**。**M1 进度**：**所有可自动单元已合**（UI-01/02/03a/03b/03d-1/03d-2/03e/03f/04）；剩 **03c（legal，owner-gated）** = M1 收口最后一块。⚠️ **SEO 翻旗部署门（owner，部署时）**：merge≠prod 上线；生产把 /en 暴露给爬虫须 owner 部署，go-live 前 gate = AnonymousTrialPanel flag-ON 时 /en 首页仍泄漏中文（UI-03g）+ en-support 声明 `availableLanguage=en-US`（03a 已合，inert）待确认。

**新增延后项（UI-03b 发现，待排期）**：
- **AnonymousTrialPanel 本地化（独立单元，暂记 UI-03g）**：`anonymous-trial-panel.tsx`（954 行 / 119 CJK / flag `NEXT_PUBLIC_ENABLE_ANONYMOUS_PREVIEW`-ON 的匿名预览上传·预览·**consent 法务文案**漏斗）。consent 文案有法务权重，应与 03c legal 同等审慎，不混入 leak-fix。**因该 flag-ON 时 /en home 泄漏中文，@codex #66 P2-1 + 项目主决策已把 home `/` 移出 `localizedRoutes`（#67）→ /en home 不挂 en hreflang / 不进 sitemap en。** ⚠️ **UI-03g 落地（本地化 panel）后必须配套**：① `site.ts` 把 `/` 加回 `localizedRoutes`；② 移除 `hreflang-check.mjs` 的 home `/` 无-en 防回归断言 + `zh-snapshot.mjs` 的 `hreflang('/')` zh-only 断言（改回互惠含 en）；③ home `page.tsx`/`sitemap.ts` 的 `homeLocalized`/`localized` 分支自动恢复挂 languages（无需改）。
- **UI-03d 范围补充（@codex #53 P2 采纳）**：03d 不止 per-page `generateMetadata`，**还须 locale 化面包屑 JSON-LD（BreadcrumbJsonLd item 名）**——否则 `html lang="en"` 下 /en 的 `<title>`/分享预览/结构化数据仍中文。

### Wave U-B — Phase 2 工作台（**枚举占位，§9.2 决策门**）

| 单元 | 文档 | 状态 | 母方案 Phase | 工时 | 门 |
|---|---|---|---|---|---|
| UI-05 | [App 中央字典本地化](UI-05-central-dictionaries.md) | ⏸ | Phase 2 · T2.1 | M | §9.2 Q1/Q2 |
| UI-06 | [always-on 用户页](UI-06-app-user-flows.md) | ⏸ | Phase 2 · T2.2 | L | §9.2 + **与 TU-11/PR#38 协调** + 与 UI-BE-01 同排 |
| UI-07 | [Intl 格式器参数化](UI-07-intl-formatters.md) | ⏸ | Phase 2 · T2.3 | S | §9.2 |

### Wave U-C — Phase 3 共享 UI + 错误层（**枚举占位，§9.2 决策门**）

| 单元 | 文档 | 状态 | 母方案 Phase | 工时 | 门 |
|---|---|---|---|---|---|
| UI-08 | [共享 UI 收尾](UI-08-shared-ui.md) | ⏸ | Phase 3 · T3.3 | M | §9.2 |
| UI-09 | [客户端错误层 + error-code map](UI-09-client-error-layer.md) | ⏸ | Phase 3 · T3.4 | M | §9.2 + 耦合 UI-BE-01 |

### 后端轨 — Phase 4（**指针，非前端 uiloc lane**）

| 单元 | 文档 | 状态 | 母方案 Phase | 工时 | 门 |
|---|---|---|---|---|---|
| UI-BE-01 | [后端 error-code envelope + Accept-Language](UI-BE-01-backend-error-codes.md) | ⏸ | Phase 4 | L | §9.2 Q7；归后端 backlog / 与代码质量后端轨协调 |

### 非单元（显式 out-of-scope，防误领）

- **Admin**（`app/[locale]/(app)/admin/**`）— operator-only，保持中文，不翻（随 route group 整体物理迁移但不动内容）。
- **post-edit 子树**（`workspace/[jobId]/edit/**`）— `NEXT_PUBLIC_ENABLE_POST_EDIT` 默认关，随该功能上线再议。
- **币种换算 / USD 定价显示** — 归 Paddle/billing 后续（母方案 §1.6 / §9.2 Q4）。
- **配音 `target_language`** — 归 multilingual-mutual-translation 工作（§0.1）。

## 文档直达

- M1：[UI-01 基础设施](UI-01-i18n-foundation.md) · [UI-02 路由迁移](UI-02-locale-routing-migration.md) · [UI-03 营销英文化（a/b/c/d 四子单元，见文内「子单元拆分」）](UI-03-marketing-en-seo.md) · [UI-04 最小 Auth](UI-04-min-auth-en.md)
- Phase 2：[UI-05 中央字典](UI-05-central-dictionaries.md) · [UI-06 用户页](UI-06-app-user-flows.md) · [UI-07 Intl 格式器](UI-07-intl-formatters.md)
- Phase 3：[UI-08 共享 UI](UI-08-shared-ui.md) · [UI-09 客户端错误层](UI-09-client-error-layer.md)
- 后端轨：[UI-BE-01 后端 error-code](UI-BE-01-backend-error-codes.md)

## 依赖关系（DAG）

```
UI-01 ──→ UI-02 ──┬──→ UI-03a ──→ UI-03b ──┐
  (P0a)   (P0b)    │      (结构化)   (hero/pricing)│
                  │    UI-03c（legal 人审，并行）  ├──→ UI-03d（SEO 翻旗，依 03a+03b）
                  │                              │
                  └──→ UI-04   （最小 Auth，依 UI-02）
                        UI-03* / UI-04 可在 UI-02 合并后并行；UI-03c 人审慢不阻塞 03a/b/d
            │
            └─ M1 里程碑边界（停，报项目主验收）──────────────
                                                              ↓ §9.2 决策后
UI-05 ──→ UI-06            UI-07（独立，依 UI-02）
  (中央字典先于用户页)     UI-08    UI-09 ←→ UI-BE-01（耦合）
```

- **M1 推进顺序**：UI-01 →（合并后）UI-02（**spike 先行**）→（合并后）UI-03a → UI-03b → UI-03d（SEO 翻旗），UI-03c（legal 人审）与 UI-04 并行 → **M1 边界停**。
- Phase 2/3：§9.2 拍板后开；UI-05 先于 UI-06；UI-06 须排在 **TU-11 / PR #38** 后（文件重叠，母方案 §0.5/§0.6）；UI-09 与 UI-BE-01 耦合（前端 code→串 等后端发 code）。

## 关键跨单元协调（实施前必读）

1. **UI-01 ↔ UI-02 紧急回滚耦合**：止血回滚（`routing.ts` 的 `locales` 收回 `['zh']`）改的是 **UI-01 文件**——UI-02 紧急回滚须与 UI-01 owner 协调。
2. **UI-02 ↔ UI-03 两处 owner 边界**：(a) title 模板**品牌后缀** locale 化归 **UI-02** 的 `[locale]/layout`，UI-03 只改 page 级 title 值，**避免双后缀**；(b) 字体变量 `--font-en-display` 注入点在 `[locale]/layout`，由 **UI-02** 拥有，UI-03 **append 不重排**。
3. **`<html lang>` zh-CN→zh-Hans 单点豁免（UI-02）**：是有意变更，会在「zh 字节一致快照」上产生 **1 处预期 diff**——须**显式登记单点豁免并经项目主确认**，不得静默掩盖红线 1。
4. **messages 形态统一（CodeX 二审 #1）**：M1 全线 **namespace-per-file** `messages/{zh,en}/{common,marketing,auth,seo}.json`；UI-01 的 `request.ts` 用**固定 namespace import + merge**（非 glob、非单文件）。UI-03 写 `marketing`/`seo`、UI-04 写 `auth`，与 UI-01 骨架一致。**任何单元都不得退回单文件 `messages/{locale}.json`**（会导致命名空间文件运行时不加载）。
5. **无 JS 测试运行器 → 统一 `uiloc:*` 脚本（CodeX 二审 #5）**：`frontend-next/` 无 vitest/jest（CI frontend job = lint + tsc + build）。守卫一律独立 node 脚本 + `npm run`，**统一命名**：`uiloc:cjk-guard` / `uiloc:zh-snapshot` / `uiloc:key-parity`（UI-01 立机制，UI-02/03/04 复用 + 按需 append 如 `uiloc:hreflang-check`）。**不得**写 `vitest`/`jest`/`npm test -- <name>` 等不存在的命令。
6. **post-UI-02 路径**：UI-03/UI-04 在 UI-02 之后执行，`(marketing)`/`(auth)` 已在 `app/[locale]/` 下；其文档为可读性仍写 `app/(marketing|auth)/...`，执行者 Step 0 须解析为 `app/[locale]/(...)`。
7. **paddle-checkout root layout（CodeX 二审 #3）**：UI-02 删顶层 `app/layout.tsx` 后，`app/paddle-checkout/` 必须自带 `app/paddle-checkout/layout.tsx`（locale-neutral root layout，多 root layout 模式），否则 build 报错——已写进 UI-02 Step 7（非 §9 决策）。
8. **shallow repo footgun**：本仓库 `.git` 是 shallow + 多 worktree 共享——**本地 shell 永不** `git fetch --depth=1`（会 graft HEAD 进共享 `.git/shallow` 致 push 被 reject）。守卫的 base-ref diff **只在 CI** 用 TU-03 的浅 fetch 模式（memory `feedback_shallow_repo_fetch_footgun`）。
9. **共享 CI/pre-commit append 不覆盖**：`.github/workflows/ci.yml` + `.pre-commit-config.yaml` 由代码质量 TU-03 建好；本系列只 **append**（复用「只阻断新增 / 读 base-ref 基线」模式），动前 rebase 最新 main（母方案 §0.6）。

## 统一文档模板（每个 flesh 单元遵循）

```
# UI-NN · <标题>
- 执行框架说明（ship-unit / checkbox / worktree / 显式 pathspec）
- 元信息表：目标 / 价值 / 关联(母方案 Phase+§) / 前置依赖 / 建议分支 / 预估工时
- ## 不在本单元范围（out-of-scope）
- ## 必守不变量（复述本单元相关红线）
- ## 执行步骤：Step 0 确认现状 → Step 1..N（每步：动作 + 文件 + 具体改法 + 该步验收[可机器验证命令]）
- ## 测试计划（新增 + 默认 zh 字节一致回归）
- ## 回滚方案（文件 / commit 边界 / git revert 优先）
- ## 完成定义 (DoD)（清单式可勾选）
- ## 关联（回母方案 + 依赖单元）
```

## 全局必守红线（所有单元通用）

- **付费 API 硬约束**：本系列**纯表现层**，**不得**新增/触碰任何 MiniMax clone / 付费 TTS / 付费 LLM / 付费 ASR 路径。
- **红线 1 默认 zh 字节一致**：每单元交付后默认中文 DOM/URL/SEO 标签逐字节不变（`<html lang>` zh-CN→zh-Hans 的单点豁免见上「关键协调 3」）。
- **红线 2 不碰 pipeline 语言字段**：UI locale ≠ `target_language`；不读写 `source_language`/`target_language`/`language_pair`/`cn_text`/voice `compatible_target_languages`。
- **红线 4 SEO anti-leak**：`canonical`/`hreflang`/OG title·description 永在 page 级，绝不进 root/`[locale]` layout。
- **红线 5 content 不译**：job 标题、转录、译文、voice 名、`display_title_zh`、说话人名一律透传。
- **红线 8 不自动重定向 + `localeDetection:false`**：URL 是唯一语言真源，cookie 不驱动裸 `/` 恢复。
- **单 `<html>`**：`<html>/<body>` 唯一落 `app/[locale]/layout.tsx`，顶层 `app/layout.tsx` 删除。
- **git**：独立 worktree + `uiloc/...` 分支；提交显式 pathspec（`git commit -- <files>`），**禁** `git add .`。

## 实施 LOG

| 日期 | 单元 | PR | 审查 | 结果 |
|---|---|---|---|---|
| 2026-06-25 | UI-01 i18n 基础设施 | [#46](https://github.com/sun9bear/AITransVideo/pull/46) squash `050722b2` | CodeX CLI（v4 AppConfig typo 保护，已修+探针验证）→ 多 lens（zh-snapshot node floor / cjk-guard fail-closed，已修）→ @codex bot（2×P2 守卫硬化：occurrence-count + env-independence，已修验证；1×P1 lockfile=false-positive，npm ci 实测 exit 0 已驳回）→ CI blocking 全绿 | ✅ 合并 main |
| 2026-06-25 | UI-02 路由迁移 + proxy + 切换器 | [#48](https://github.com/sun9bear/AITransVideo/pull/48) squash `56404a55`（rebase 过 TU-07） | 多 lens 8-agent 对抗评审：**1 critical**（/paddle-checkout 被 next-intl rewrite 进 /zh → authed 支付 handoff 404；`next start` 实测确认）+ **1 high**（workspace/[jobId]/page.tsx 单引号 import 被双引号 sed 漏切→丢 /en 前缀）→ 均修+运行态复验。@codex bot：2×P2（登录/登出后 locale 连续性）→ **转 UI-04**（UI-02 Step 3 显式把 window.location/登出留原生）。gate 全绿：build/lint(0 err)/cjk-guard(重生成多集证明纯 relocation 2447 occ net-new=0)/key-parity/zh-snapshot；proxy curl 矩阵全过（R8 不跳、auth locale 保留、authed paddle-checkout 200） | ✅ 合并 main |
| 2026-06-25 | UI-04 最小 Auth 英文化 | [#49](https://github.com/sun9bear/AITransVideo/pull/49) squash `85f2e7c3` | 实现委派子 agent（4 commit）+ 我独立复跑全 gate。多 lens 6-agent 对抗评审 **0 critical/high/medium，7 low**（均聚 post-auth-redirect.ts）→ 收敛：回环守卫 locale-aware（`/en/auth/login` 漏 `startsWith('/auth')`→en 漏斗登录回环；deLocalizePath 剥前缀后判，from verbatim，7-case 逻辑测过）+ 中文 throw/zh-snapshot 值级局限标注转 UI-09。@codex bot **0 findings**（tsc⚠=其 sandbox next-intl 解析失败=false neg，本地+CI tsc 绿）。gate 全绿：lint(0err)/tsc/build(8 路由 SSG)/key-parity(152)/cjk-guard(net-new=0,移除182occ)/zh-snapshot(含 auth 字节一致) | ✅ 合并 main |
| 2026-06-25 | UI-03a 营销结构化文案抽取 | [#50](https://github.com/sun9bear/AITransVideo/pull/50) squash `f30c1506` | 委派子 agent 实现（FAQ/对比表/nav/footer/SEO chrome 入 marketing+seo namespace；FAQ items 单源同喂 DOM+JsonLd；site-json-ld locale 驱动 zh 保 zh-only）。我独立复跑全 gate + 多 lens 4-agent 对抗评审 **0 critical/high/medium，5 low** → 收敛 en seo 双源同步守卫（messages/en/seo.json↔localeSeo.en）。@codex 0 findings。**移交/待确认**：company-info 常量随消费点 03b/03c 抽；seo namespace inert→03d 收敛单源；🔶 en JSON-LD availableLanguage=en-US 待项目主确认是否真有英文客服（03d go-live 前，否则去 en-US 保 ['zh-CN']） | ✅ 合并 main |
| 2026-06-25 | UI-03b hero/pricing/trial 内联重文案 | 分支 `uiloc/marketing-en-seo-b` 已推 `9c02c4f0`（未开 PR） | 委派子 agent 实现（hero rich-text/pricing 链路 ICU/trial + company-info 在 pricing-assurance 消费点字典化）。**我已独立复跑全 gate 全绿**：tsc 0 / lint 0err / build(pricing·trial SSG) / key-parity / cjk-guard(net-new=0，多集 2192→2120 移除72) / zh-snapshot(+14 串)。**未做：多 lens 对抗评审 + push-PR + @codex + merge** —— 本会话上下文已很深，按既定纪律停在干净检查点交新会话续（不降质硬推）。子 agent 2 裁定待评审核：①company-info 常量保留(仍被 03c legal/contact 消费)②trial-details.tsx 判 out-of-scope(卡只列 trial/page，需核是否致 trial 页半中半英) | ◐ 实现完成待评审 ship |
| 2026-06-26 | **CodeX 审核 + 范围修正**（见上「⚠️ 范围修正」§） | — | CodeX [P1]：UI-03b 非「实现完成」——`/en` 页仍露中文(TrialDetails/CTA)，应作 PR 前阻塞项。核查发现**原方案卡漏列首页 ~9 区块 + CTA + trial-details**，`/en` 首页主体仍中文(UI-03a+b 仅 ~40% 营销)。[P2] UI-03b 落后 main(TU-08/#51)，PR 前须 rebase。项目主决策：①补全整个 /en 营销→新增 UI-03e/f ②百度网盘从条款删除(通用第三方存储措辞兜底)。**纠正前述「M1 实现完成 4/4」高估** | 🔧 范围扩大，replan |
| 2026-06-27 | **UI-03b 收尾 + ship** | [#53](https://github.com/sun9bear/AITransVideo/pull/53) squash `c26bd43e` | rebase 到 main `228bc275`(整合 PayPal billing namespace) + 收尾 commit。**6 leak 组件**字典化(trial-details/primary-cta/plan-card-cta/anonymous-trial-launcher【占位分支】/**hero-sample-player/seal-stamp** 后两者原卡又漏列、核 hero 渲染链揪出)。我独立复核：zh 字节一致 **21/21**、cjk-baseline 只减不增(2120→2078)、全 gate 绿。**多 lens 5-agent 对抗评审 0 confirmed**。@codex **3×P2 均已知/有意出范围**(AnonymousTrialPanel 延后 + metadata/breadcrumb→03d)、**无 major**。**file-size-guard 红=PayPal 撑大 billing.py 的 main-state(非本 change)→ PR #54 `e44999d3` bump 基线后 re-run 转绿**（教训：CI 推理对 **origin/main** 非本地 worktree——本地 main 有别轨未推 credits commit 误判多 2 violator）。passthrough 守住:plan.display_name/demo.*/译印章 PNG | ✅ 合并 main |
| 2026-06-27 | **UI-03e 首页上半区英文化** | [#60](https://github.com/sun9bear/AITransVideo/pull/60) squash `8ddf8690` | 4 组件（pain-points/product-proof/workflow-showcase server→async getTranslations + featured-demos-client/featured-demo-card client useTranslations）→ 4 新 namespace。委派子 agent 实现 + 我独立复核：**全 50 条 zh 字典叶值 = HEAD 源逐字节一致**（自动核验，超出 9 条 snapshot pin）、cjk-baseline 只减不增(2078→2023, 0 net-new)、全 gate 绿(含 build 双 locale)。**多 lens 5-agent 对抗评审 0/0 confirmed**。@codex 无 major（⚠️=其 sandbox 缺 typescript dep，false neg）。passthrough：demo.display_name/品牌词/注释不动 | ✅ 合并 main |
| 2026-06-27 | **UI-03f 首页下半区英文化** | [#61](https://github.com/sun9bear/AITransVideo/pull/61) squash `d94f2099` | 5 组件（features/suited-scenarios/trust-banner/pricing-preview/final-cta 全 server→async getTranslations + t.raw；final-cta h2 t.rich br；features/trust-banner icon 移出为 index 数组）→ 5 新 namespace。我独立复核：**全 51 条 zh 叶值 = HEAD 源逐字节一致**（final-cta rich heading 经 JSX 空白折叠等价复核）、cjk-baseline 2023→1971(0 net-new)、全 gate 绿。**多 lens 5-agent（含 icon-pairing-order 专项）0/0 confirmed**。@codex「No blocking findings」。**至此 /en 首页全区内容英文化（除 SEO/metadata=03d）**。只消费不改 03b/03e 子组件；link-button 无 CJK 不动 | ✅ 合并 main |
| 2026-06-27 | **UI-03d-1 营销 SEO 翻旗** | [#63](https://github.com/sun9bear/AITransVideo/pull/63) squash `1e93d6fc` | home/pricing/trial（已整页英文）`static metadata → generateMetadata(locale)`：self-canonical（zh 保留相对串 byte-identical / en 绝对 /en）+ 互惠 hreflang + localized OG；`site.ts` 加 `localizedRoutes` + `hreflangLanguages` 路由感知（legal 只挂 zh）；layout 仅本地化 title 模板/keywords/OG locale（**R4：无 canonical/hreflang/OG-url**）；sitemap 单点走 hreflangLanguages；breadcrumb 加可选 locale prop；**新增 `hreflang-check` guard**；zh-snapshot 翻旗 `hreflang('/')` 含 en + 新增 `hreflang('/terms')` 无 en 防回归。委派子 agent + 我独立复核：6 条 zh seo 值 = HEAD 源逐字节一致、R4 grep clean、cjk-baseline 只减、全 gate 绿。**多 lens 5-agent SEO 对抗评审（hreflang 自指/R4/R1/Next16/guard）2 raw→0 confirmed**（仅注释计数 nit，已修）。**@codex 用量耗尽**（rate-limited）→ codex 限额 fallback：主模型 + 5-lens 终审。legal 页不动、留 UI-03c | ✅ 合并 main |
| 2026-06-27 | **UI-03d-2 EN 排版轨道** | [#64](https://github.com/sun9bear/AITransVideo/pull/64) squash `d3036b83` | /en `.ink-display` 换 EB Garamond 800（复用已加载变量字体，800 与 600 同 woff2=零新增二进制）替代 CJK 回退 Georgia faux-bold + 收紧 `.zh-body` 行距；layout 转 async+getLocale，`locale==="en"` 给 `.marketing-root` 追加 ` locale-en`（zh className byte-identical）；globals.css 沿用 `.ink-heading` 同款「单类+:not(.__never__)」(0,3,0) 规避 Tailwind v4 class+attr 剥离。**built CSS 确认规则命中未剥离**；zh 无 .locale-en 不命中（R1 安全）；全 gate 绿；独立 review agent 0 findings。**@codex rate-limited**→主模型+review agent 终审 | ✅ 合并 main |
| 2026-06-27 | **@codex 补审 #63 SEO 翻旗 + 2 followup** | review-only [#66](https://github.com/sun9bear/AITransVideo/pull/66)（已关）→ [#67](https://github.com/sun9bear/AITransVideo/pull/67) `aa09bfeb` + [#68](https://github.com/sun9bear/AITransVideo/pull/68) `db4e1f75` | @codex 用量恢复后补审已合的 03d-1。**经验：merged PR + 非 main-base PR @codex 都不触发**——故开 review-only PR #66（base=合并前 `087e6fce`、head=`1e93d6fc` 重现 03d-1 diff）才出 review。**2×P2，无 critical/high**：**P2-1**（home `/` 挂 en hreflang，但 `NEXT_PUBLIC_ENABLE_ANONYMOUS_PREVIEW=1` 时 AnonymousTrialPanel 让 /en home 泄漏中文）→ 项目主决策「先 drop /en home」→ **#67**：`localizedRoutes` 移除 `/`；@codex 二审 followup① 又抓出 /en home 残留 broken zh-only alternate set → **followup②** 改为 home/legal 在 page+sitemap **均不挂 hreflang languages**（仅 localizedRoutes 挂），新增 `hreflang-check` home no-en 防回归。**P2-2**（pricing/trial SEO description 硬编码 Free/Plus/Pro/180min/无需绑卡 = gateway 真源漂移）→ **#68**：采纳方案 2 去事实化（`seo.json` zh+en + `zh-snapshot` pin；当前 main 描述在 seo.json、cjk-guard 不扫 messages → 仅 3 文件，无需改 .tsx/baseline）。@codex 对 #67 followup②/#68 均「Didn't find any major issues」。**教训**：spawned task 从主 checkout 起（stale `f1eb671f` −5 PR）会基于旧码、diff 作废 → spawned task 须从 `origin/main` 起 | ✅ 均合并 main |
| — | **续接指引（新会话）**：**M1 仅剩 UI-03c legal（owner 人审门，不自动合）** = M1 收口最后一块。按 [UI-03c-legal-human-review.md](UI-03c-legal-human-review.md) 项目主决策修 3 blocker 中文源(terms 7.2 非续费 / privacy 删百度网盘+通用第三方存储兜底措辞 / refund 9.2 原路退回)；terms 15.2 管辖 + 续费措辞留项目主/律师定稿；改后 cjk-baseline 重生成 → en legal 翻译 → **二轮人审 + 项目主签字** → 合并（届时 legal 页一并 generateMetadata + 入 `localizedRoutes` 挂 en hreflang）。**UI-03g（AnonymousTrialPanel 本地化）落地须配套把 home `/` 加回 `localizedRoutes` + 还原 home hreflang 守卫**（见上「新增延后项」§）。**SEO 部署门**（owner，部署时非合并时）：生产暴露 /en 须 owner 部署；go-live 前确认 AnonymousTrialPanel flag 状态 + en-support 声明。 | — | — | ⏭ 仅剩 03c |
