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
| UI-03a | [营销·结构化文案抽取](UI-03-marketing-en-seo.md)（§子单元拆分） | ☐ | Phase 1·T1.1 | M | 是（依 UI-02） | `uiloc/marketing-en-seo-a` |
| UI-03b | [营销·内联重文案 hero/pricing/trial](UI-03-marketing-en-seo.md) | ☐ | Phase 1·T1.2 | M | 是（依 UI-03a） | `uiloc/marketing-en-seo-b` |
| UI-03c | [营销·legal 人审](UI-03-marketing-en-seo.md)（HARD 人审） | ☐ | Phase 1·T1.2 | M | 是（**并行，不阻塞 3a/b/d**） | `uiloc/marketing-en-seo-c` |
| UI-03d | [营销·EN 排版 + SEO 翻旗](UI-03-marketing-en-seo.md) | ☐ | Phase 1·T1.3 | M | 否（依 UI-03a+b 合并） | `uiloc/marketing-en-seo-d` |
| UI-04 | [最小 Auth 英文化](UI-04-min-auth-en.md) | ☐ | Phase 1.5 | M | 是（依 UI-02） | `uiloc/min-auth-en` |

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
| — | （续 → UI-03a/b/c/d 营销 + UI-04 最小 Auth，M1 边界停） | — | UI-02 已落地 [locale]+proxy+navigation+切换器，UI-03/04 解锁可并行 | ⏭ 下一步 |
