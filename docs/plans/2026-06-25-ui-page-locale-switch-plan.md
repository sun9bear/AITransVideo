# 界面语言切换方案（UI Page Locale）— 中文默认 + 英文，可扩展多语言 v1

> **For agentic workers:** REQUIRED SUB-SKILL: 用 superpowers:writing-plans / superpowers:executing-plans 逐 Task 实施。Steps 用 checkbox（`- [ ]`）追踪。**本文件只是方案，暂不动代码**（用户指令）；实施前需项目主批准 §9 开放问题。

**Goal:** 给本产品的 **网站/工作台界面**（chrome：导航、按钮、标签、表单、状态、提示）增加「中文 ⇄ 英文」整页语言切换，并把 i18n 基础设施做成「以后加日/韩/… 只是改一个 locales 数组」的可扩展底座。默认中文（zh）行为与今天**逐字节一致**（zero-regression），英文（en）按价值分阶段铺开。

**Architecture:** 前端表现层 + 路由层改造，引入 `next-intl` v4 作为 App-Router/RSC 原生 i18n 运行时；`app/[locale]/` 动态段统一三个 route group；默认 zh 不带前缀、en 走 `/en` 子路径；`proxy.ts`（Next 16 新约定，原 `middleware.ts`）承载 locale 检测/重定向并与既有 canonical+auth 逻辑合并；消息抽进 `messages/{zh,en}/*.json` 键值目录；切换器复用 `next-themes` 风格的 cookie + RSC 读取模型。**后端 server-emitted 中文文本（error detail / 邮件 / 客服 / 公告）是独立的跨栈后续轨，不在前端阶段射程内**（§1.9 / Phase 4）。

**Tech Stack:** Next.js 16.2.1 App Router + React 19.2 + TypeScript strict + Tailwind v4 + shadcn/ui；`next-intl@^4`（新增）；现有 `next-themes`（switcher 模型参考）。

**版本记录:** v1 2026-06-25 初稿（证据基线 = 10-agent 并行调查，前端 8 surface + 外部 2 research，1.4M tokens；结论经主模型复核 Next 16 `middleware→proxy` 迁移文档、prices 动态来源、中央字典 seam、与 `target_language` 轴区分）→ **v2 2026-06-25 CodeX 外审修正**：①`localeDetection: false` 显式化（§1.2a，对齐红线 8）②root/`[locale]` layout 单 `<html>` 结构定死 + 实测项（§1.5）③P0 拆 **P0a 非结构 / P0b 结构迁移**（§3/§4）④前移 **Phase 1.5 最小 Auth 英文化**（营销漏斗不断）⑤client bundle 按 namespace 切、server-first `getTranslations`（§1.4）⑥CJK lint 改 baseline-snapshot 排除注释（§5）⑦P2 英文工作台必须与 Phase 4 高频 error-code 同排期（§3/§9）→ **v3 2026-06-25 CodeX 二审修正**：①顶层 `app/layout.tsx` **删除**（非 `return children`），`[locale]/layout` 作唯一 root layout 含唯一 `<html>/<body>`，迁移清单写进 Task 0.2（§1.5）②`localeDetection:false` 后 cookie **不参与**裸路径语言判定，URL 是唯一真源（§1.5/Task 0.4）③messages 用**具体 import** 非 glob，起步单文件 `messages/{locale}.json`（§1.4/Task 0.1）④验收改「`/sitemap.xml` 含 zh/en alternates」，**不要求** `/en/sitemap.xml`（Task 0.3）⑤Phase 3 表/DoD 去掉 auth/captcha（已前移 Phase 1.5）→ **v4 2026-06-25 CodeX 二审·backlog 接口对齐**（拆 backlog 后发现的跨单元接口漂移，主方案随附修正）：①messages 定为 **namespace-per-file** `messages/{zh,en}/{common,marketing,auth,seo}.json` + 固定 namespace import+merge——**推翻上面 v3 ③「起步单文件」**（单文件会让 UI-03/04 的 namespace 文件运行时不加载）②加 `paddle-checkout/layout.tsx` 后口径改为「`[locale]/layout` 是**本地化主子树** root layout；`paddle-checkout/layout` 与 `global-*` 各自是合法独立 root/壳」——**修正上面 v3 ②「唯一 root layout」**（§1.5 / UI-02 Step 7）③UI-03 拆 UI-03a/b/c/d（legal=03c 独立人审、不阻塞）④守卫统一 `uiloc:cjk-guard`/`uiloc:zh-snapshot`/`uiloc:key-parity`（无 vitest/jest）⑤UI-03/04 路径口径=post-UI-02（`app/[locale]/(...)`）。**Status: `NOT_STARTED`**（设计文档，无实现）。

> **CodeX 复审一处精确化（v2）**：CodeX 拍板「暂缓统一 `[locale]` 大迁移」需澄清——**结构迁移（P0b：三个 route group 迁入 `app/[locale]/`）是 SEO-correct `/en` 营销的地基，不可延后**；真正可延后的是**工作台的翻译工作量**（P2）。即「一次性迁结构，分批翻内容」，不是「延后迁结构」。迁移后 `/en/workspace` 等会以 zh fallback 渲染（页面在、内容未译），这没问题（且已在 auth 后、noindex）。详见 §1.2 / §9。

---

## 0. 背景与现状（评审者/实施者必读）

### 0.1 ⚠️ 两条 i18n 轴的硬区分（本方案最重要的前置）

本仓库里「i18n / 多语言」有**两个正交含义，绝不可混**：

| 维度 | 本方案（UI page locale / 界面语言） | 产品 target_language（配音方向） |
| --- | --- | --- |
| 含义 | 操作者看到的**界面 chrome** 用什么语言（按钮/导航/标签/提示） | 视频被**配音/翻译成**哪种语言（en→zh / zh→en …） |
| 归属文档 | **本文件** | `docs/plans/2026-04-15-i18n-target-language-direction.md`（`DEFERRED`）+ 执行基线 `docs/plans/2026-06-13-multilingual-mutual-translation-plan-v3.md` |
| 实现状态 | 不存在，net-new | PR-A 已并入 main（language_registry / alembic 036 / Job.source_language·target_language·language_pair / 权益 gate / admin flag `language_pairs_enabled` StrictBool 默认 False）；PR-W/B/H/CD/E/F/G 在未合并分支（`claude/ml-integration` / 草稿 PR #38），整体 gated OFF，默认 en→zh 路径字节一致 |
| 拥有的概念 | message catalog / locale cookie / hreflang / `<html lang>` | SemanticBlock、`cn_text`（canonical 目标文本容器）、`voice_catalog.compatible_target_languages`、翻译指纹冻结、付费 API fail-closed 路由 |

**硬不变量（任何 Task 不得违反）：切 UI locale 永不读写任何 pipeline 语言字段。** 即不得触碰 `source_language` / `target_language` / `language_pair` / `cn_text` / voice `compatible_target_languages`，不得影响 create-job 的 pair gate，不得改变 GA 的 en→zh 字节一致路径。`TranslationForm` 里「配音目标语言」选择器是**内容控制（content）**，与右上角的「界面语言」切换器是两个独立的东西。说话人显示名、`display_title_zh`、转录/译文产物等都是 **content**，**永不**进 UI locale 翻译射程。

> 命名也要避让：已有 `...i18n-target-language-direction.md` 与 `...multilingual-mutual-translation-plan-v3.md`。本方案文件名刻意用 `ui-page-locale-switch`，标题用「界面语言切换」，不用裸 token `i18n` / `target-language` / `multilingual`。

### 0.2 现状盘点（证据来自 10-agent 调查）

- **零 i18n 基础设施**：`frontend-next/package.json` 无 next-intl / react-i18next / i18next / lingui / formatjs。无 message catalog、无 `t()`、无 locale context、无 locale 路由。
- **硬编码规模**：ripgrep CJK（U+4E00–U+9FFF）命中 **5,147 行 / 165 个文件**（共 223 个 ts/tsx）。注意此数含**代码注释**，会高估真实可译字符串数（如 `admin/settings/page.tsx` 471 行多为 Pydantic-sync 注释）。
- **`<html lang>` 与 OG locale 硬编码**：`app/layout.tsx:66` `<html lang="zh-CN">`、`:44` `openGraph.locale: "zh_CN"`、title 模板 `"%s · 爱译视频 AITrans.Video"`、中文 keywords[]。
- **App 层有「中央字典」seam（关键省力点）**：状态/阶段/错误 chrome 大量经少数文件汇聚——
  - `frontend-next/src/types/jobs.ts` → `JOB_STATUS_LABELS`（11 个 job 状态）
  - `frontend-next/src/features/jobs/presentation.ts` → `stageLabels`（9 阶段）/ `reviewStageDescriptions` / 错误分类 `{label,suggestion}` / `sanitizedProgressMessages`（已做 EN→zh 映射且**故意丢弃**含 `Web UI`/`fallback`/`legacy` 的内部串）/ 一组 `getStageLabel`/`getReviewPrompt`/`getErrorCategory`/`getJobDisplayTitle` helper
  - `frontend-next/src/features/jobs/stageMetadata.ts` → 阶段描述
  - `frontend-next/src/features/jobs/expiry.ts` → `即将删除` / `N 天后过期`（模板/复数）
  - `frontend-next/src/components/status-badge.tsx` → 消费 `JOB_STATUS_LABELS` + 内联 `重合成中 · 第 N 次修改`
  - 翻译这 ~4 个文件即可本地化全 App 大部分状态/阶段/错误 chrome。
- **价格是动态数据，不是硬编码**：`lib/billing/get-plans.ts::getPlansSafeServer()`（gateway `GET /api/plans`，SSR）是唯一来源；`pricing-grid.tsx` 本地 `formatYuan(fen)`→`¥`、单位 `/ 月 /季 /年`、benefit 串内插 gateway 数字。**locale 切换只改标签/单位格式，不改数字真值，不做币种换算**（gateway 只返回 `price_cny_fen`）。
- **server-emitted 中文（前端无法独立修）**：gateway `detail="<中文>"` 约 **239 处 / 27 模块**（auth/billing/job_intercept/voice/support/admin…），无 error code、无 Accept-Language；前端多处 `toast.error(data?.detail || "<中文兜底>")` 直接渲染。另有客服 chatbot 文案、邮件模板（`auth_email.py _email_html`）、公告/通知自由文本。详见 §1.9。
- **路由/中间件现状**：Next 16.2.1，`output:'standalone'`，三个 route group（`(marketing)`/`(app)`/`(auth)`）+ 非分组独立路由（`paddle-checkout`）+ special files（`sitemap.ts`/`robots.ts`/`not-found`/`error`）。`src/middleware.ts` 已承载 ①canonical-origin 308 重定向 ②session-cookie auth gate（带 `publicExactPaths`/`publicPaths` 白名单）。前面是 Caddy（`/api`/`/job-api`/`/gateway`/部分 `/auth` API → Gateway:8880，其余 → Next:3000）。
- **provider 先例**：`components/providers/session-provider.tsx`（cookie/`/auth/me` + context）+ `app-shell.tsx`（运行时给 `documentElement` 盖 `data-theme`/`.dark`，但**不碰 `lang`**）是 LocaleProvider 的现成模板；`next-themes` 已装但当前 inert。`components/support/support-copy.ts` 是「集中文案模块」的现成范式。

### 0.3 设计红线（任何 Task 不得违反）

1. **zh 默认字节一致**：每个阶段交付后，默认中文渲染（DOM + URL + SEO 标签）必须与改造前逐字节相同。未迁移的串以 zh-CN 为 fallback 正常显示。
2. **UI locale 不碰 pipeline 语言字段**（§0.1 硬不变量）。
3. **presentation-only，不碰付费 API**：本工作纯表现层，**不得**新增/改变任何 TTS/clone/LLM/ASR 付费路径；遵守 CLAUDE.md 付费 API 硬约束。
4. **保留 SEO anti-leak 不变量（GEO plan §7.4）**：canonical / hreflang / OG title·description **永不**放进 root layout（否则泄漏进 `/workspace`/`/admin` 误标记）；只让 `<html lang>` 和 title 模板的品牌后缀变成 locale 感知。
5. **content 永不翻译**：job 标题、转录/译文、voice 名、`display_title_zh`、说话人名、剪映/产物文本一律 verbatim 透传；只翻周边 chrome（如 `未命名视频` fallback、`YouTube 视频 ·` 前缀）。
6. **保留 `sanitizedProgressMessages` 的 null-过滤语义**：迁移后仍要隐藏含 `Web UI`/`fallback`/`legacy` 的后端内部串，不得借 i18n 改造把内部措辞重新暴露。
7. **不为 noindex 页加 hreflang**：app/auth 已 noindex，绝不给它们打 hreflang/alternate。
8. **不按检测语言自动重定向**：用非重定向切换器（Google 明确警告自动按语言/地域跳转会让爬虫看不到全部版本）。

### 0.4 约定变更（需在本方案落地时同步改 CLAUDE.md）

CLAUDE.md「Key Conventions」现有一条 **「所有 UI 文本和沟通用中文」**，字面禁止非中文 UI，会**自相矛盾地阻塞**本方案。落地 Phase 0 时把该条改写为：

> 所有 UI 文本经本地化层（next-intl message catalog）产出；**zh-CN 是默认/源 locale**；新 UI 串必须是 message key，**不得**新写内联 CJK 字面量（由回归守卫强制，见 §5）。沟通/文档默认仍用中文。

### 0.5 与未合并产品 i18n 分支的文件协调

`claude/ml-integration` / 草稿 PR #38（产品 target_language 工作）会改 `frontend-next/src/lib/api/mappers.ts`、`TranslationForm.tsx`、`SegmentRow.tsx`。本方案 Phase 2 也会动 `TranslationForm.tsx`。**协调原则**：本方案的 App 层（Phase 2/3）排在那些分支合并之后，或与项目主约定非重叠改动/文件 owner，避免并行重写同一文件造成 merge 冲突（见 CLAUDE.md 多-agent worktree 协作模型）。营销层（Phase 1）与那些分支无重叠，可先行。

### 0.6 与「代码质量治理（TU-*）」并行协调（2026-06-25 评估）

母方案 [`2026-06-24-code-quality-optimization-plan-MERGED.md`](2026-06-24-code-quality-optimization-plan-MERGED.md) + [`code-quality-tasks/TU-00-INDEX.md`](code-quality-tasks/TU-00-INDEX.md) 正由另一会话分单元实施（Wave A/B 的 TU-01/03/04/05 已合 main）。**本方案 M1 可与之并行**：

- **源文件几乎零重叠**：TU 剩余单元绝大多数是 Python 后端/gateway/DB（TU-06/07/08/09/12/13/14/15/16/17）+ 部署（TU-02，已确认**不碰** `next.config.ts`/前端）；本方案 M1 全在 `frontend-next/` 的 TS/React。
- **唯二前端 TU = TU-10（编辑页 shell 化 FE-001/002）/ TU-11（语音选择共享 FE-004/009）映射到本方案 Phase 2（已延后），不在 M1**。其中 TU-10 改的正是本方案 OUT-OF-SCOPE 的 post-edit 编辑页——先 shell 化反而利于以后 i18n。
- **git 模型天然支持**：TU-00-INDEX 用的就是「各自独立 worktree + feature 分支」；本方案在 `claude/ui-page-locale-*` 自己的 worktree 干活，显式 pathspec、不 `git add .`、不与 TU agent 共用工作树。

**协调点（少量）：**
1. **共享配置文件** `.github/workflows/ci.yml` + `.pre-commit-config.yaml`（TU-03 已合 main 建好脚手架）：P0a 的 CJK-lint / 前端 type-check **append 到既有脚手架**（不覆盖），动前先 rebase 最新 main；**复用 TU-03 的「只阻断新增 / 读 base-ref 基线」模式**做 CJK 守卫，与既有 ruff/file-size-guard 风格一致。`CLAUDE.md` 约定变更改的是 Key Conventions 节，与 TU 改的 git/架构节不同区，冲突小。
2. **Phase 2（非 M1）前端重叠**：将来做 Phase 2 时 VoiceSelectionPanel 与 **TU-11** 重叠、TranslationForm 与产品 **PR #38** 重叠——届时排在其后或约定文件 owner（§0.5）。**不影响 M1**。

**结论：M1 现在即可独立开工，无阻塞。**

---

## 1. 技术选型与架构决策

### 1.1 库选型：`next-intl` v4

| 选项 | 结论 | 理由 |
| --- | --- | --- |
| **next-intl v4** | ✅ **选它** | 唯一为 App Router + RSC 而生：服务端 `getTranslations`（async Server Component，零 client JS）+ 客户端 `useTranslations`（单一 `NextIntlClientProvider`）；`setRequestLocale` 支持静态渲染；自带 Next 16-aware proxy；ICU message（解决数字内插/复数）；TS 强类型 messages |
| next-i18next | ❌ | Pages-Router 时代，App Router 基本失维 |
| react-i18next / i18next | ❌ | client-first，RSC 要手搓 server instance，丢静态渲染 |
| lingui | ❌ | RSC/App-Router 集成弱，macro 编译步骤与 TS-strict + Turbopack 摩擦 |
| Next 内置 i18n routing | ❌ | 仅 Pages Router，App Router **未移植**（官方让你自己实现或用库） |

版本：**`next-intl@^4`**（v4 才有 Next-16/proxy 指南与 cookie 行为；v3 不感知 Next 16）。

### 1.2 路由策略：`app/[locale]/` 段 + `as-needed`，默认 zh 不带前缀

**决策：统一 `[locale]` 动态段包住三个 route group，`localePrefix: 'as-needed'`，`defaultLocale: 'zh'`，`locales: ['zh','en']`。**

- zh（默认）：`/`、`/pricing`、`/workspace/[id]` —— **不带前缀**，与今天一致（红线 1）。
- en：`/en`、`/en/pricing`、`/en/workspace/[id]`。
- 多余的 `/zh/...` 自动 301 到 `/...`。
- 加 ja/ko 只是改 `locales` 数组（默认 zh 永远裸路径，其余带前缀）。

**为什么不是 cookie-only / 同一 URL 切换**：Google 爬虫从美国出、不带 `Accept-Language`、不读 cookie——同一 URL 双语言会让它**只索引一种**，另一种永不进索引（两个 research agent 一致判定 SEO-fatal）。营销层必须可被双语分别索引，故子路径是硬要求。

**为什么 app/auth 也统一进 `[locale]`（而非营销子路径+app cookie-only 的混合）**：app/auth 本就 noindex，`/en/workspace` 没有 SEO 成本；统一 `[locale]` 是 next-intl 的 happy path，比「混合两套策略」简单得多，且是「未来多语言」最干净的底座。代价是要把三个 route group 物理迁到 `app/[locale]/` 下（一次性结构改动，见 §2、风险 R1）。

> **澄清（CodeX v2）**：这个结构迁移**不可延后**——只要想要 SEO-correct 的 `/en` 营销，`[locale]` 段就必须包住所有 route group（混合「营销进 `[locale]`、app 留扁平」会产生 `/pricing` vs `[locale]` 动态段的路由二义性，是 next-intl 不支持的脆弱配置，**不建议**）。所以 P0b 一次性迁完结构，`/en/workspace` 等先以 zh fallback 渲染，**翻译**工作量（P2）才是真正可分批/延后的部分。

### 1.2a localeDetection 必须显式关闭（CodeX v2 补强）

`next-intl` 的 routing 默认 `localeDetection: true`——middleware 会用 `NEXT_LOCALE` cookie + `Accept-Language` 头**自动**把用户从默认裸路径重定向到带前缀路径。这**违反红线 8 且 SEO 有害**（美国爬虫/英文浏览器访问 `/` 可能被 307 到 `/en`，Google 明确警告不要按语言自动跳转）。

**决策：`defineRouting({ ..., localeDetection: false })`。** 含义与取舍：

- `/` **永远**确定性渲染中文（不因 cookie/Accept-Language 跳转），爬虫安全、默认裸路径字节一致（红线 1）。
- `/en/*` 由 URL 前缀决定，只能**显式切换**产生。
- **持久化取舍（产品决策，列 §9）**：`localeDetection: false` 下，回访用户即使上次选了 en，访问裸域 `/` 仍得中文（不自动恢复）。若要「记住我的语言」，**不得**用 middleware 自动重定向，而用**非重定向**的客户端提示（如顶部一次性「View in English? →」横幅，用户点了才去 `/en`），既尊重红线 8 又不伤 SEO。v1 默认不自动恢复，横幅作为可选增强。

### 1.3 `proxy.ts` 整合（Next 16 新约定）+ 顺序

**事实核对（已查 Next 16 升级文档）**：Next 16 把 `middleware` 文件约定**弃用并改名为 `proxy`**（`mv middleware.ts proxy.ts`，导出函数 `middleware`→`proxy`，配置 `skipMiddlewareUrlNormalize`→`skipProxyUrlNormalize`）。**注意：`proxy` 运行时是 `nodejs`、不支持 `edge`**；要留 edge 必须继续用 `middleware`。本仓库现用 `middleware.ts`（**仍可运行，仅 deprecated**），观察到 auth gate 在生产正常——所以「middleware 在 Next 16 完全不跑」的说法是**夸大**，实为弃用。

**整合方案**：把 locale 逻辑与既有 canonical+auth 合并进**单一** `src/proxy.ts`，顺序严格为：

```
canonical-origin 重定向(308)  →  locale 解析/重定向(next-intl)  →  session auth gate
```

- 用 next-intl 的 `createMiddleware(routing)` 作为 locale 层，但**手动编排**进现有函数体（不要让两个独立中间件互相打架）。
- `publicExactPaths` / `publicPaths` 白名单要 **locale-aware**：匹配前先 strip `/en` 前缀做归一化（单点 normalize，不要给每条目复制 `/en` 变体）。
- matcher 继续排除 `/api`、`/job-api`、`/_next`、静态资源（守住「不拦付费 API 代理路径」，与 next.config no-blanket-rewrite guard 一致）。
- 顺序错（locale 在 auth 之前/之后放反）会引发**重定向环 / 保护页泄漏 / 把爬虫从 `/sitemap.xml` 302 走**——务必按上面三段顺序。

### 1.4 消息目录组织 + TS 强类型

- 目录：`frontend-next/messages/{zh,en}/{common,marketing,app,auth,seo}.json`（按 route group 分 namespace）。
- **`next-intl` 加载（CodeX 二审定稿：M1 从一开始就 namespace-per-file，不用单文件/不用 glob）**：M1 用 `messages/{zh,en}/{common,marketing,auth,seo}.json`（Phase 2 加 `app`）。`src/i18n/request.ts` 的 `getRequestConfig` 用**固定 namespace import + merge**——逐个 `import(\`../../messages/${locale}/common.json\`)` …（4 个）后按 namespace 键合并成 `{ common, marketing, auth, seo }`。**不要**单文件 `messages/${locale}.json`（UI-03/UI-04 写的是 namespace 文件，单文件会让它们运行时不加载——CodeX 二审 #1），**也不要** `import(.../*.json)` glob（不可直接落地）。server 端加载完整 catalog 无所谓（不进 client）；client bundle 的瘦身在 `NextIntlClientProvider` 用 `pick(namespace)` 做（见下条与 §1.5）。详见单元 [UI-01](uiloc-tasks/UI-01-i18n-foundation.md) Step 3/6。
- **TS 类型**：用 zh JSON 声明全局 `IntlMessages`，让 `t('key')` 有补全、打错 key 编译失败（i18n 的主要 DX 收益，必须开）。
- **ICU**：所有「数字内插/复数/词序」串（`N 天后过期`、`第 N 次修改`、`等待{stage}`、trial leadParagraph、pricing benefit、`已向 {phone} 发送验证码`、`最近 {n} / 共 {m} 条`、`请求超时（{n} 秒）`）一律用 ICU message，**不得**字符串拼接（否则英文复数/语序必错）。
- **client bundle 切片（CodeX v2 补强，性能红线）**：**不要**在顶层 `NextIntlClientProvider` 一次性把 `marketing/app/auth/seo` 全部 messages 灌给 client——否则每个 client 页都背上整本字典（违反 web perf bundle 预算）。原则：① Server Component 优先 `getTranslations`（零 client JS）；② client 组件只拿**必要 namespace**——用 next-intl 的 `NextIntlClientProvider messages={pick(messages, ['app.workspace', ...])}` 只下发该子树需要的 namespace；③ namespace 粒度按 route group + 功能区切（`marketing.*` 不进 app client bundle，反之亦然）。这也反向约束 §1.4 的目录粒度：namespace 要切得够细才能精准下发。

### 1.5 LocaleProvider / 切换器 / cookie / `<html lang>`

- **provider**：克隆 `session-provider.tsx` 模式做一个轻 LocaleProvider（或直接用 `NextIntlClientProvider`），挂在 `app/[locale]/layout.tsx`。
- **`<html lang>` 用 SSR 设，且 `<html>`/`<body>` 只渲染一次（CodeX v2 定死结构）**：
  - **本地化主子树的 `<html>`/`<body>` 落在 `app/[locale]/layout.tsx`**（next-intl 官方 with-i18n-routing 结构）：从 `params.locale` 渲染 `<html lang={locale}>`（zh→`zh-Hans`、en→`en`），现 root layout 的 `.dark`/字体 `<link>`/theme-color/`SessionProvider`/`Toaster` 全部下移到这里。
  - **顶层 `app/layout.tsx` 直接删除（CodeX v3 定死，不留 `return children` 选项）**：Next 规则是 root layout **必须**含 `<html>/<body>`，所以让顶层 `return children` 是**无效 root layout**（不安全）。正确做法=**删掉 `app/layout.tsx`**，使 `app/[locale]/layout.tsx` 成为**本地化主子树的** root layout、承载其 `<html>/<body>`。**绝不**在 `[locale]` 子树内两层渲染 `<html>`。
  - **合法多 root layout（CodeX 二审 #1，删顶层 layout 后的必需结构）**：`[locale]` **之外**的独立路由需各自的 root——`app/paddle-checkout/layout.tsx`（locale-neutral root layout，UI-02 Step 7 当场建）与 `global-not-found`/`global-error` 壳**各自含 `<html>`**。故口径不是「全树唯一 `<html>`」，而是「**本地化子树**只在 `[locale]/layout` 一处 `<html>`；独立 root/壳合法另算」。
  - **`[locale]` 之外的 special files**（`app/sitemap.ts`/`robots.ts` 无 DOM 不受影响；`app/not-found.tsx`/全局 error 需要自带 `<html>` 壳）按 next-intl 文档的 global-not-found 处理；`paddle-checkout` 见 §9。
  - **绝不**学 `app-shell.tsx` 用 client effect 盖 `lang`——SSR/hydration lang 不一致且伤 SEO。
  - **⚠️ 实测项**：**P0b 实施时对 Next 16.2.1 实跑确认**——删除顶层 `app/layout.tsx` 后 `[locale]/layout` 作为 root layout 被正确接受、`global-not-found` / 全局 error 的最终写法，不要照搬旧教程。
- **切换器 UI**：放 `components/marketing/site-header.tsx` 右侧簇（已是 client component），镜像 `next-themes` 的日/月 toggle 交互；用 next-intl `navigation` 的 `useRouter().replace(pathname,{locale})` 切换；app 层在 AppShell/顶栏也放一个。
- **持久化（CodeX v3 修正：URL 是唯一语言真源）**：`localeDetection:false` 下，**语言只由 URL 前缀决定，cookie 不参与裸路径 `/` 的语言判定、不驱动任何自动重定向**。cookie（`NEXT_LOCALE`，普通 cookie 非 localStorage）只作「偏好提示 / 切换器状态」——切换器可手动写它，但 `/` 永不因 cookie 自动恢复成 en（要恢复用 §1.2a 的非重定向横幅）。`localeCookie.maxAge=1 年` 仅为让横幅/切换器记住偏好；**不要**把它描述成「优先于 Accept-Language 参与解析」（那是 `localeDetection:true` 的语义，已弃用）。
- **v1 仅 cookie/URL**，不写用户 profile；登录态把 locale 同步到后端 profile 列为后续（§9）。

### 1.6 货币与数字格式

- **保留 CNY ¥ 真值**：数字来自 gateway `price_cny_fen`，locale 切换**只**翻标签/单位（把 `/ 月` 等与 benefit 模板做 per-locale ICU），`¥` 符号保留。
- **不做币种换算**（gateway 无 USD 数据）。英文用户看 USD 是**商业决策**，归 Paddle MoR（已处理 USD）的后续，列入 §9，不在本方案。
- 把硬编码的 `new Intl.DateTimeFormat('zh-CN')` / `toLocaleDateString('zh-CN')`（`projects`、`settings`）参数化为 active locale。

### 1.7 英文排版轨道（ink typography）

- 现状：`.ink-display`/`.ink-heading` 为 CJK 的 `Noto Serif SC`（系统/本地栈，**未**经 next/font 加载）调；`.marketing-root .ink-heading` 前置 `var(--font-eb-garamond)` 给拉丁；`.zh-body` line-height 1.75 + letter-spacing 是为 CJK 调的，英文会显松。
- 方案：`globals.css` 加 EN 轨道——locale=en 时为 `.ink-display` 经 next/font 载一款拉丁展示衬线（Noto Serif SC 不含拉丁且未打包）；加 `.en-body` 收紧 line-height/letter-spacing。英文文案普遍比中文长 1.5–2×：hero h1 的手动 `<br>`、header CTA 按钮、`tool-comparison` 网格、pricing 卡片要在 320/768/1024/1440 重测 overflow/wrap。

### 1.8 SEO 本地化（营销层）

每个**公开营销页**，按 locale 设：

- `alternates.canonical` = **本 locale 自指** URL（en 页 canonical 指自己，绝不指 zh，否则自我 de-index）。
- `alternates.languages` = 互惠 hreflang map：`{ 'zh-Hans': zhUrl, 'en': enUrl, 'x-default': zhUrl }`（**恰好一个 x-default 指 zh 主市场**；hreflang 必须双向 + 自指，否则 Google 整簇忽略）。
- 本地化 `title` / `description` / `openGraph.title|description`；`openGraph.url` = 本 locale URL；`openGraph.locale` = `zh_CN`|`en_US`。
- `openGraph.alternateLocale` / `og:locale:alternate`：**Next 16.2 metadata 不支持**（vercel/next.js #58903 仍 open），需要就在 JSX 手写 `<meta property>`；但其 SEO 价值边际，优先把 hreflang 做对即可。
- **JSON-LD `inLanguage`**：`site-json-ld.tsx` 现硬编码 `zh-CN`（两处）+ `availableLanguage:['zh-CN']`，改成 active locale 驱动 + 双语；`breadcrumb-json-ld` 的 `name`（`首页`/`定价`…）与 `faq-json-ld` 的 Q/A 要 per-locale，且与可见 DOM **字节对齐**（Google 结构化数据要求）。
- **`sitemap.ts`**：每条 `publicRoutes` 用 `alternates.languages` 发 zh/en 互链（Next 14.2+ 自动产出 `xhtml:link` + namespace）；保留「不写 lastmod」决策（别用 `new Date()` 撒谎）。
- **`robots.ts` / `blockedRoutes`**：若 app 路由出现 `/en` 前缀，disallow 也要覆盖 `/en/workspace` 等（现仅匹配裸 `/workspace`），否则登录页面在 en locale 下对爬虫暴露。
- `lib/seo/site.ts` 加 locale 维度：`siteName`/`defaultTitle`/`defaultDescription`/`keywords`/品牌后缀做 per-locale record，加 `absoluteUrl(path, locale?)` 与「返回某 path 的 hreflang map」helper。**保持 `siteUrl` 与 gateway `SITE_URL` 同源不变**（site.ts §6.4 契约）。
- **先 inert 后翻旗**：先把 hreflang/canonical/OG/JSON-LD 管线铺成 zh-only（languages map 只含 zh），en 内容+proxy 落地后再打开 en URL——沿用本项目「inert flag 先行」纪律。

### 1.9 server-emitted 文本边界（前端能修 vs 需后端）

- **前端阶段可修**：所有前端自有 chrome、`stageMetadata.ts` 阶段标签、`toast.error(... || "<中文>")` 里的**客户端兜底**串（换成本地化串）、货币/日期格式。
- **需后端（独立 Phase 4 / 跨栈）**：UI 直接渲染 `error.detail` 的地方（登录/注册/job/billing/voice/support toast）显示的是后端发来的中文——前端只能靠「忽略 detail，用 HTTP status + error code 映射本地化串」来 mask，而这需要后端**发稳定 error code**（现多为裸中文 prose 无 code）或 **honor Accept-Language**，两者今天都没有。邮件模板、客服 chatbot、存储的公告/通知是纯 server-rendered content，前端完全无法本地化。
- **前端阶段策略**：引入「优先 error-code/HTTP-status → 本地化 message map」的错误呈现层（推广 `password-login-form.tsx` 已有的 `LOGIN_ERROR_MESSAGES` 模式 + `client.ts` 已有的 `error_code`/`detail.message` 解包），把 raw `detail` 降为最后兜底。**坦白承认**：未编码的后端错误在前端阶段仍会漏中文，直到 Phase 4 后端加 code/Accept-Language。

---

## 2. 目录/文件结构变更总览

```
frontend-next/
  proxy.ts                         # ← 由 middleware.ts 改名并入 locale 逻辑（§1.3）
  next.config.ts                   # ← createNextIntlPlugin 包裹（保留 output:standalone + 现有 rewrites/guard）
  messages/
    zh/{common,marketing,app,auth,seo}.json
    en/{common,marketing,app,auth,seo}.json
  src/
    i18n/
      routing.ts                   # defineRouting({locales:['zh','en'],defaultLocale:'zh',localePrefix:'as-needed',localeCookie:{maxAge:31536000}})
      request.ts                   # getRequestConfig → 动态 import messages
      navigation.ts                # createNavigation(routing) → 本地化 Link/redirect/useRouter/usePathname
    app/
      layout.tsx                   # ← 退化为最小外壳（<html> 下移到 [locale]/layout）
      [locale]/
        layout.tsx                 # ← 设 <html lang={locale}> + setRequestLocale + generateStaticParams + NextIntlClientProvider
        (marketing)/ …             # ← 三个 route group 物理迁到 [locale]/ 下
        (app)/ …
        (auth)/ …
      sitemap.ts / robots.ts       # ← locale-aware
      paddle-checkout/             # 独立路由：决定是否进 [locale]（§9）
    components/
      i18n/LocaleSwitcher.tsx      # 新增切换器（site-header / app 顶栏复用）
      providers/locale-provider.tsx# 可选，克隆 session-provider 模式
    lib/seo/site.ts                # ← 加 locale 维度 + absoluteUrl(path, locale)
```

> `app/[locale]/` 之外的独立路由（`paddle-checkout`）与 special files（`not-found`/`error`）需显式决定 locale 行为（§9 / Phase 0）。

---

## 3. 分阶段交付计划

| 阶段 | 范围 | 价值 / 风险 | 默认 zh 影响 |
| --- | --- | --- | --- |
| **P0a 非结构基础设施** | 装 next-intl、`i18n/` helpers、messages 骨架、SEO helper（inert）、约定变更、CJK lint baseline；**不动路由结构**（zh 单 locale，可先 no-routing 模式验证 catalog） | 低风险、可独立合并、不碰 auth 白名单/Caddy | 字节一致（红线 1）|
| **P0b 结构迁移** | 三个 route group 迁入 `app/[locale]/`、`proxy` 合并（canonical→locale→auth）、`<html lang>` SSR 单壳、`localeDetection:false`、切换器、SEO 管线接 `[locale]`（仍 inert） | **最大单点风险**（动路由结构）；独立一批做 | 字节一致（红线 1）|
| **Phase 1 营销层 EN + SEO** | `(marketing)` 全量翻译 + hreflang/canonical/OG/JSON-LD/sitemap/robots + EN 排版轨道 + 切换器/SEO 翻旗 | 最高获客价值、SEO 关键、与 PR#38 无重叠 | zh 不变 |
| **Phase 1.5 最小 Auth 英文化（CodeX v2 前移）** | `auth/login`、`auth/register`（先 dedupe）、`forgot-password`、captcha provider locale、这些流的客户端错误兜底 | **必做**：否则 `/en` 营销 CTA 点进去就是中文登录→漏斗断 | zh 不变 |
| **Phase 2 App 核心用户流** | 先中央字典（`JOB_STATUS_LABELS`/`presentation.ts`/`stageMetadata`/`expiry`）；再 `TranslationForm`、workspace 审校路径、`projects`/`voices`/`settings`/`help`/`billing`；ICU 化模板串；参数化 Intl 格式器 | 让英文用户能真正用产品；**⚠️与 Phase 4 高频 error-code 同排期**（否则工作台 toast 全中文）；与 PR#38 重叠需协调（§0.5）| zh 不变 |
| **Phase 3 共享 UI + 错误层收尾** | shadcn a11y 串、`confirm-dialog`/`empty-state`/`log-viewer` 默认 props、`support/**` 残留内联串、客户端错误兜底集中本地化、推广 error-code map（**auth/captcha 已在 Phase 1.5 完成，不在此**）| 闭合用户可见前端面 | zh 不变 |
| **Phase 4（后续·独立后端轨）** | gateway error-code envelope `{code,detail}` + Accept-Language；邮件/客服/公告本地化 | 跨栈、payment/auth 敏感、回归面大 | — |

**明确 OUT OF SCOPE（v1）**：

- **Admin（`app/(app)/admin/**` ~22 文件）**：operator-only、面积最大且注释膨胀——保持中文，不翻；除非引入非中文运营者。
- **post-edit 子树（`workspace/[jobId]/edit/**`）**：`NEXT_PUBLIC_ENABLE_POST_EDIT` 默认关，随该功能上线再翻。
- **币种换算 / USD 定价显示**（§1.6，归 Paddle 后续）。
- **产品 target_language / 配音方向**（§0.1，归 multilingual-mutual-translation 工作）。

---

## 4. Tasks

> Phase 0/1 细化到可实施；Phase 2/3 给 Task 级 outline（逐串翻译在实施时进行，本方案不写译文）。每个改动用**显式 pathspec** commit（CLAUDE.md），不 `git add .`。建议在隔离 worktree + feature 分支 `claude/ui-page-locale-*` 干活。

### P0 — 基础设施（**拆 P0a 非结构 + P0b 结构迁移**，CodeX v2）

> **P0a = Task 0.1 / 0.5 / 0.6**（装库、catalog、SEO helper inert、约定、lint —— 不动路由结构，可先合）。
> **P0b = Task 0.2 / 0.3 / 0.4**（`[locale]` 迁移、proxy 合并、`<html lang>`、切换器 —— 单独一批，最大单点风险）。
> P0a 可用 next-intl「no-routing」模式（固定 zh）先验 catalog/`getTranslations`，零 URL 改动；P0b 再切到 i18n routing 引入 `/en`。

#### Task 0.1（P0a）：装 next-intl + i18n 配置骨架
**Files:** Create `src/i18n/routing.ts`、`src/i18n/request.ts`、`src/i18n/navigation.ts`；Modify `package.json`、`next.config.ts`
- [ ] 装 `next-intl@^4`。
- [ ] `routing.ts`：`defineRouting({locales:['zh','en'],defaultLocale:'zh',localePrefix:'as-needed',localeDetection:false,localeCookie:{maxAge:60*60*24*365}})`。**`localeDetection:false` 必写**（§1.2a：默认 true 会按 cookie/Accept-Language 自动重定向，违反红线 8）。
- [ ] `request.ts`：`getRequestConfig`，`hasLocale` 校验 + **固定 namespace import + merge**（`messages/${locale}/{common,marketing,auth,seo}.json` 逐个 import 后合并，非单文件、非 glob；见 §1.4 / UI-01 Step 3）。
- [ ] `navigation.ts`：`createNavigation(routing)` 导出本地化 `Link/redirect/usePathname/useRouter/getPathname`。
- [ ] `next.config.ts`：`createNextIntlPlugin('./src/i18n/request.ts')` 包裹，保留 `output:'standalone'` 与现有 dev rewrites + no-blanket-rewrite guard。

#### Task 0.2：`[locale]` 段 + 三 route group 迁移 + `<html lang>` SSR
**Files:** Create `app/[locale]/layout.tsx`（本地化主子树 root layout）；Move `app/(marketing|app|auth)/**` → `app/[locale]/(...)/**`；**Delete** `app/layout.tsx`（内容迁入 `[locale]/layout`）；Create `app/paddle-checkout/layout.tsx`（locale-neutral 独立 root layout）；Create `app/global-not-found.tsx`/全局 error 壳（按实测）
- [ ] 三个 route group 物理迁到 `app/[locale]/` 下（group 可嵌套在动态段内）。
- [ ] `app/[locale]/layout.tsx` **成为本地化主子树 root layout**：`generateStaticParams()=routing.locales.map(l=>({locale:l}))`；进函数先 `setRequestLocale(locale)`；渲染本地化子树的 `<html lang>`（`zh→zh-Hans`/`en→en`）+ `<body>`；包 `NextIntlClientProvider`（`messages` 用 `pick` 只下发必要 namespace，§1.4/§1.5）。
- [ ] **建 `app/paddle-checkout/layout.tsx`**（locale-neutral 独立 root layout，自带 `<html lang="zh-Hans">/<body>`）——删顶层 layout 后，`[locale]` 外的 `paddle-checkout` 必须自带 root（多 root layout 模式），否则 build 报错（CodeX 二审 #3）。
- [ ] **删除顶层 `app/layout.tsx`**（CodeX v3：不留 `return children`，那是无效 root layout）。把现 root layout 的这些一并迁入 `[locale]/layout.tsx`：`metadataBase`、Search Console verification（google/msvalidate）、字体 `<link>`、`.dark`/theme-color、`SessionProvider`、`Toaster`、skip-to-main。**canonical/OG-locale 仍只在 page 级，不进此 layout**（守红线 4）。
- [ ] 迁移 `[locale]` 之外的 special files：`app/not-found.tsx` / 全局 error 改为自带 `<html>` 壳的 `global-not-found`/`global-error` 写法（按 Next 16.2.1 实测）；`app/sitemap.ts`/`robots.ts` 留在 `app/` 顶层不动（仍是 `/sitemap.xml`、`/robots.txt`）。
- [ ] 所有内部 `<Link href>`/`router.push` 改用 `src/i18n/navigation` 的版本（否则导航丢 locale）。
- [ ] 每个静态页在任何 `t()` 前调 `setRequestLocale`（漏了会静默退化为 dynamic 渲染）。

#### Task 0.3：proxy 合并（canonical → locale → auth）
**Files:** Rename `src/middleware.ts`→`src/proxy.ts`；Modify 内容
- [ ] `mv middleware.ts proxy.ts`，导出 `proxy`；`next.config` 里相关 flag 改名（如有）。注意 `proxy` 仅 `nodejs` 运行时——确认现 middleware 无 edge-only 依赖（auth/canonical 用的是普通 cookie/header 读取，nodejs 兼容）。
- [ ] 在函数体内编排：先 `canonicalRedirect` → 再 next-intl `createMiddleware(routing)` 的 locale 解析/重定向 → 再 auth gate。
- [ ] `publicExactPaths`/`publicPaths` 匹配前做单点 `stripLocalePrefix` 归一化。
- [ ] matcher 保持排除 `/api`/`/job-api`/`/_next`/静态资源/`favicon`。
- [ ] 回归测：未登录访问 `/en/workspace` 仍 302 到登录；**`/sitemap.xml` 200 且含 zh/en `alternates`**（单一 root sitemap，**不要求** `/en/sitemap.xml`——除非专门加 `app/[locale]/sitemap.ts`，否则它不存在，CodeX v3）；`/robots.txt` 200；无重定向环。

#### Task 0.4：LocaleSwitcher + cookie 持久化
**Files:** Create `components/i18n/LocaleSwitcher.tsx`；Modify `site-header.tsx`、app 顶栏
- [ ] 切换器用 `navigation.useRouter().replace(pathname,{locale})` 走 **URL 前缀**切换（语言真源是 URL，不是 cookie）。
- [ ] 偏好 cookie **手动写**（如需「记住偏好」）；`localeDetection:false` 下 cookie 不驱动 `/` 自动恢复（§1.5）。可选：非重定向「View in English?」横幅（§1.2a）。
- [ ] 交互镜像 `next-themes` toggle；可访问性（aria-label、键盘）。

#### Task 0.5：catalog 骨架 + SEO 管线 inert + site.ts locale 维度
**Files:** Create `messages/{zh,en}/*.json`（先空/占位）；Modify `lib/seo/site.ts`、`app/sitemap.ts`、`app/robots.ts`、`components/seo/*`、各营销页 `generateMetadata`
- [ ] `site.ts` 加 per-locale record + `absoluteUrl(path, locale)` + hreflang-map helper（**languages map 先只含 zh，inert**）。
- [ ] 营销页 metadata 改为 `generateMetadata({params})`：自指 canonical + （inert 的）languages map + OG locale。
- [ ] JSON-LD `inLanguage` 改 locale 驱动；sitemap 加 `alternates`（inert）；robots 覆盖 `/en` 前缀。
- [ ] **守 GEO §7.4**：这些只在 page 级，不进 root layout。

#### Task 0.6：约定变更 + 回归守卫
**Files:** Modify `CLAUDE.md`；Create lint/test guard、zh-snapshot 测试
- [ ] 改写 CLAUDE.md「所有 UI 文本用中文」为 §0.4 的本地化层规则。
- [ ] 加「禁止新内联 CJK 字面量」的 lint/test（扫 `src/**/*.tsx` 的 JSX text/字符串字面量，allowlist 既有未迁移文件，新增即红——见 §5）。
- [ ] 加「默认 zh 渲染字节一致」验收测试（关键页 DOM snapshot）。

### P1 — 营销层 EN + SEO

#### Task 1.1：抽取已结构化文案 → 字典
**Files:** `messages/{zh,en}/marketing.json`、`seo.json`；Modify `faq.tsx`、`tool-comparison.tsx`、`company-info.ts`、`site-header.tsx`(NAV_ITEMS)、`site-footer.tsx`、`lib/seo/site.ts`
- [ ] 先迁「已是 const 数组/对象」的（faq、ROWS、company-info、NAV、SEO 串）——最干净的 beachhead。FAQ 的可见 Q/A 与 `faq-json-ld` **同字典同 locale**，保持字节对齐。

#### Task 1.2：内联 JSX 重文案页迁移
**Files:** `hero.tsx`、`(marketing)/page.tsx`、`pricing/page.tsx`、`trial/page.tsx`、legal 各页（`terms/privacy/refund/contact`）、`pricing-grid.tsx`、`trial-banner.tsx` 等
- [ ] hero/pricing/trial：内插数字的串用 ICU（`planBenefits()`/`leadParagraph` 改 per-locale ICU 模板，词序/量词正确）。
- [ ] 货币：`formatYuan`/单位/benefit 走 §1.6（保 ¥，只翻标签）。
- [ ] legal 正文密集且法务敏感——**人审翻译**，不用纯 MT。legal 页已有 `titleEn` 装饰副标，可顺势升级为真翻译。

#### Task 1.3：EN 排版轨道 + 翻旗 SEO + 布局 QA
**Files:** `globals.css`、`(marketing)/layout.tsx`、`lib/seo/site.ts`
- [ ] 加 `.en-*` 排版轨道 + next/font 拉丁展示衬线（§1.7）。
- [ ] 把 §0.5 inert 的 hreflang/sitemap languages map 打开含 en。
- [ ] 320/768/1024/1440 布局 QA（hero `<br>`、header CTA、comparison 网格、pricing 卡片 overflow/wrap）；Search Console hreflang 互惠/x-default/自指校验。

### P1.5 — 最小 Auth 英文化（CodeX v2 前移，与 Phase 1 同里程碑）

> 理由：`/en` 营销页的 CTA（登录/注册/免费试用）落到 `/auth*`，若仍是中文，英文获客漏斗在注册口就断。**不做完整工作台，但注册入口必须英文化。**

- **Task 1.5.1**：先 dedupe `app/(auth)/auth/page.tsx` 与 `auth/register/page.tsx`（近重复），再抽串到 `messages/{zh,en}/auth.json`。
- **Task 1.5.2**：翻 `auth/login`、register（phone/email 两表单 + `register-method-form`）、`forgot-password`；倒计时/手机号回显/秒数串用 ICU。
- **Task 1.5.3**：`captcha-gate.tsx` 的 GeeTest `language:'zho'` / Turnstile `language:'zh-cn'` 改 locale 驱动（否则英文表单里嵌中文验证码）。
- **Task 1.5.4**：这些 auth 流的客户端兜底串（`data?.detail || "<中文>"` 里的兜底）换本地化串。**注意**：服务端 `detail` 仍是中文（需 Phase 4 后端 code），此处只能 mask 客户端兜底；登录失败等真实后端错误英文化要等 Phase 4——Phase 1.5 验收时知会项目主此缺口。

### P2 — App 核心用户流（Task 级 outline）

- **Task 2.1 中央字典本地化**：`types/jobs.ts`(`JOB_STATUS_LABELS`)、`presentation.ts`(`stageLabels`/`reviewStageDescriptions`/错误分类/**保 `sanitizedProgressMessages` null-过滤**)、`stageMetadata.ts`、`expiry.ts`、`status-badge.tsx` 内联模板。模板串 ICU 化（`N 天后过期`/`第 N 次修改`/`等待{stage}`）。这步本地化全 App 大部分状态/阶段/错误 chrome。
- **Task 2.2 always-on 用户页**：`TranslationForm.tsx`（核心转化流，含民法典1023 consent——人审）、workspace 审校路径（`workspace/[jobId]/page.tsx`、`VoiceSelectionPanel`/`VoiceReviewPanel`/`TranslationReviewPanel`/`VoiceCloneModal`/Smart* 面板/`ResultMediaCard`）、`projects`、`voices`、`settings`+`settings/billing`、`help`、`notifications`、`components/billing/**`。
- **Task 2.3 格式器参数化**：`projects`/`settings` 的 `Intl.DateTimeFormat('zh-CN')`/`toLocaleDateString('zh-CN')` 改 active locale。
- **content 边界**：`getJobDisplayTitle`/`getJobSecondaryLabel` 只翻 chrome（`未命名视频`、`YouTube 视频 ·` 前缀），用户内容透传（红线 5）。

### P3 — 共享 UI + 错误层（Task 级 outline）

> **注**：auth 页面全量翻译 + dedupe + captcha locale（原 Task 3.1/3.2）已**前移到 Phase 1.5**。P3 收尾共享 UI 与客户端错误层。

- **Task 3.3 共享 UI**：shadcn a11y 串（`Close`/`Toggle Sidebar`…，今天就是英文，统一进字典）、`confirm-dialog`（`请确认/确定/取消` 默认改 locale-aware）、`empty-state`(`页面提示`)、`log-viewer`（默认 props + 级别 map）、`session-provider` 内联错误、`components/support/**`（把仍内联的 banner 收进 `support-copy.ts` 风格字典）。
- **Task 3.4 客户端错误层**：把 `lib/api/client.ts`(`statusFallbackMessage`/timeout/`stringifyErrorDetail`) + `lib/api/errors.ts` 默认 + 各表单 `|| "<中文>"` 兜底集中到**单一可本地化模块**；推广 `LOGIN_ERROR_MESSAGES` 成共享「server error-code → 本地化串」map，raw `detail` 降为最后兜底（衔接 Phase 4 后端 code）。

---

## 5. 回归守卫（CI 级，永久）

1. **禁新内联 CJK（baseline-snapshot 式，CodeX v2 收紧）**：
   - 用 **AST**（非裸正则）只扫 **JSX text 节点 + 面向用户的字符串字面量**；**排除代码注释 / JSDoc**（仓库中文注释极多，扫注释会变噪音机）。
   - 生成「既有 CJK 占用」**baseline 快照**（按 occurrence 记录），守卫只对**新增未登记**的 UI 内联 CJK 报红；迁移完成的串从 baseline 移除（baseline 只减不增）。
   - **排除 out-of-scope 目录**：`app/(app)/admin/**`（不翻）、`workspace/[jobId]/edit/**`（flagged off）。
   - **允许 content/domain 术语**：voice 名、`display_title_zh`、品牌词等非 chrome 内容 allowlist 放行。
   - 目的：防止「翻一半又长回来」，同时不阻塞合法的中文注释/admin/content。
2. **默认 zh 字节一致**：关键页（home/pricing/workspace 详情/login）渲染 DOM snapshot，确保 locale 改造未改变默认中文输出。
3. **hreflang 互惠校验**：测 sitemap/metadata 的 languages map 双向 + 恰好一个 x-default + 自指。
4. **`sanitizedProgressMessages` 过滤不破**：保留原测——含 `Web UI`/`fallback`/`legacy` 仍返回 null。
5. **content 不被翻译**：测 `getJobDisplayTitle` 等对用户内容透传，仅 chrome 本地化。

## 6. 回滚顺序

依赖链：消费方依赖底座。**回滚逆序**——
- Phase 内：先回滚翻旗（SEO en URL / switcher 可见性）→ 再回滚内容字典 → 最后回滚框架（`[locale]` 段 / proxy）。
- 跨 Phase：Phase 1/2/3 各自可独立回滚到「框架在、该面仍 zh」；Phase 0 框架是被依赖方，最后回。
- 紧急止血：把 `routing.ts` 的 `locales` 收回 `['zh']`（en 路径 404/重定向回 zh），保留框架不动——en 内容/SEO 全部 inert，zh 不受影响。

## 7. 验收标准（DoD）

- **P0a**：catalog/`getTranslations` 在 zh 单 locale 下可用（no-routing 模式）；CJK-lint baseline 守卫上线；约定变更落 CLAUDE.md；零 URL 改动、zh 字节一致。
- **P0b**：`/` 与 `/en` 都能渲染；切换器经 **URL 前缀**来回切（不依赖 cookie 自动恢复）；本地化子树 `<html lang>` SSR 正确（无 hydration mismatch、`[locale]` 子树内无双 `<html>`；`paddle-checkout/layout`+`global-*` 各自独立 root 合法）；`localeDetection:false` 实测裸 `/` 不被 Accept-Language/cookie 跳转；双 root layout build 绿；`/sitemap.xml` 含 zh/en alternates；默认 zh 字节一致测试绿。
- **Phase 1**：营销 7 页 en 全译（legal 人审）；Search Console hreflang 校验通过（互惠/x-default/自指）；4 断点布局无 overflow；JSON-LD `inLanguage`/FAQ 双语字节对齐；zh SEO 标签未变。
- **Phase 2**：中央字典 + always-on 用户页 en 化；ICU 模板英文语法正确；日期/数字按 locale；content 透传；与 PR#38 无 merge 冲突。
- **Phase 1.5**：`auth/login`/register/forgot en 化；`/auth` 与 `/auth/register` 去重；captcha widget 跟随 locale；客户端兜底串 en（服务端 detail 仍待 Phase 4）。
- **Phase 3**：共享 UI（shadcn a11y/confirm-dialog/empty-state/log-viewer/support 残留）+ 客户端错误兜底集中 en 化（**auth/captcha 不在此，已在 Phase 1.5**）。
- **Phase 4（独立）**：高频用户路径后端 error-code envelope + 前端 code→本地化映射闭合。

## 8. 风险登记

| # | 风险 | 缓解 |
| --- | --- | --- |
| R1 | `[locale]` 段需物理迁三个 route group + 改全部 Link，churn 大、易碰 auth 白名单/SEO 白名单（皆为扁平字面 path） | Task 0.2/0.3 一次性做；navigation 统一替换 + proxy 单点 normalize；强回归测 |
| R2 | proxy 顺序错 → 重定向环 / 保护页泄漏 / 爬虫被 302 | 固定 canonical→locale→auth 顺序；Task 0.3 回归用例 |
| R3 | `<html lang>` 走 client effect → SSR/hydration 不一致 + 伤 SEO | 红线：lang 必须 SSR 在 `[locale]/layout` 设 |
| R4 | server-emitted 中文（239 detail/邮件/客服/公告）前端阶段仍漏中文 | §1.9 坦白；error-code map 兜底；Phase 4 独立后端轨 |
| R5 | 模板串字符串拼接迁移 → 英文复数/语序破 | 一律 ICU，不拼接（§1.4）|
| R6 | 与未合并 PR#38 改同一前端文件（mappers/TranslationForm/SegmentRow）冲突 | §0.5 排序/文件 owner 协调；营销层先行 |
| R7 | 英文文案 1.5–2× 长 → 布局破（hero `<br>`/CTA/网格/卡片） | EN 排版轨道 + 4 断点 QA（Task 1.3）|
| R8 | 误把 content（job 标题/`display_title_zh`/voice 名/转录）拉进翻译 | 红线 5 + 守卫 5 |
| R9 | 误把 UI locale 与 `target_language` 耦合 | §0.1 硬不变量 + review gate |
| R10 | next-intl/Next16 细节（**`localeDetection` 默认 true→自动跳转**、`proxy` nodejs-only、`setRequestLocale` 漏调退化 dynamic、cookie 默认 session、client 全量 messages 撑大 bundle、两层 `<html>`、OG alternateLocale 不支持）| §1.2a/§1.3/§1.4/§1.5/§1.8 已逐条记；实施按 16.2.1 实测 |
| R11 | 营销翻旗后 home 变 `/en` 影响 Search Console 验证 meta | 翻旗前确认 verification meta 仍在验证根渲染，必要时 re-verify |

## 9. 推荐首里程碑 + 待项目主决策的开放问题

### 9.0 推荐首里程碑（采纳 CodeX v2 拍板，本方案背书）

**M1 = P0a + P0b + Phase 1 + Phase 1.5**：next-intl 基础设施 + `[locale]` 结构迁移（一次性地基）+ `/en` 营销全译 + SEO hreflang + 最小 Auth 英文化。**暂缓 `/en/workspace` 翻译**（Phase 2）——结构已迁好、页面以 zh fallback 存在，等确认英文用户确实进工作台再排 P2（且 P2 须与 Phase 4 高频 error-code 同排）。把「最大单点风险=结构迁移」单独关进 P0b，其余按价值/风险渐进。

### 9.1 已在 v2 定稿的两个实施前决策（原 CodeX 要求补明确）

- **localeDetection**：✅ 定 `false`（§1.2a）。剩余 micro-decision=回访裸域是否自动恢复上次语言——v1 **不自动恢复**，可选「非重定向横幅」增强。
- **root/`[locale]` layout 结构**：✅ 定「**删除顶层 `app/layout.tsx`**，`app/[locale]/layout.tsx` 作**本地化主子树** root layout；`[locale]` 外的 `paddle-checkout/layout.tsx` 与 `global-*` 各自合法独立 root/壳」（CodeX v3+二审，§1.5 / UI-02 Step 7），P0b 对 16.2.1 实测确认双 root layout + global-not-found。

### 9.2 待项目主决策的开放问题

1. **是否采纳 9.0 的 M1 里程碑**？提醒：结构迁移本身**不可**延后（§1.2 澄清），可延后的是工作台**翻译**。
2. **英文是否一路做到核心工作台（P2）**？若要，须接受「P2 与 Phase 4 后端 error-code 同排期」的成本，否则英文工作台 toast/错误大面积漏中文。
3. **翻译来源**：法务/账单/consent 文案要人审（强烈建议）；其余 MT 辅助 + 人审还是全人审？谁负责译文与 review？
4. **英文用户币种**：v1 保持 ¥（CNY）显示，还是要 Paddle USD 定价展示（独立商业/billing 工作）？
5. **`paddle-checkout` 是否做 en→Paddle locale 映射**（产品问题）？**结构问题已在 UI-02 Step 7 解决**（CodeX 二审 #3：删顶层 layout 后给它自带 `app/paddle-checkout/layout.tsx` locale-neutral root layout，build 不受影响）；此处只剩「是否进 `[locale]` + 把界面 en 透传到 Paddle.js locale」的产品取舍。
6. **登录态 locale 是否同步到用户 profile**（跨设备记忆）？v1 仅 cookie；profile 同步列为后续。
7. **Phase 4 后端 error-code/Accept-Language 何时排期**？没有它，错误 toast 会长期漏中文——是否接受 v1 这个已知缺口。

## 10. 任务单元 backlog + 在 plans README 注册

### 10.1 ship-unit backlog（2026-06-25 已铺）

本方案已拆成可被 **ship-unit** 逐个收口的任务单元，索引见 [`uiloc-tasks/UILOC-00-INDEX.md`](uiloc-tasks/UILOC-00-INDEX.md)：
- **Wave U-A = M1（已 flesh，可派发）**：[UI-01 i18n 基础设施](uiloc-tasks/UI-01-i18n-foundation.md)（P0a）→ [UI-02 路由迁移+proxy+切换器](uiloc-tasks/UI-02-locale-routing-migration.md)（P0b，spike 先行）→ {[UI-03 营销 EN+SEO](uiloc-tasks/UI-03-marketing-en-seo.md)、[UI-04 最小 Auth EN](uiloc-tasks/UI-04-min-auth-en.md) 并行}。
- **Wave U-B/U-C（枚举占位，§9.2 决策门）**：UI-05..09。**后端轨**：UI-BE-01（指针）。
- 推进到 **M1 边界停**，报项目主验收；Phase 2+ 待 §9.2 决策后细化。

### 10.2 在 plans README 注册

落地本方案时，在 `docs/plans/README.md`：
- 「主题地图」新增一行：**界面语言切换（UI page locale）** | `NOT_STARTED` | `2026-06-25-ui-page-locale-switch-plan.md` | 备注「前端界面 i18n，与 `2026-04-15-i18n-target-language-direction.md` 的配音目标语是两条正交轴，勿混」。
- 「文件状态索引」新增：`2026-06-25-ui-page-locale-switch-plan.md` | `NOT_STARTED` | 设计文档，未实现，待项目主批准 §9。
