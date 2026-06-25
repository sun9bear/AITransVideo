# UI-03 · 营销层英文化 + SEO 翻旗 + 英文排版

> 执行单元文档。母方案 [`docs/plans/2026-06-25-ui-page-locale-switch-plan.md`](../2026-06-25-ui-page-locale-switch-plan.md)，本单元 = **Phase 1 = Task 1.1 + 1.2 + 1.3**。
> 实施前**逐字节继承**母方案决策（§1.7 / §1.8 / §0.3 红线 4·5 + Task 1.1/1.2/1.3），不得改动或与之矛盾。
> 本单元体量大——**CodeX 二审：拆成 4 个可独立成 PR 的子单元**（见下「子单元拆分」），让法务人审不卡住营销基础英文上线。

> **⚠️ 路径约定（UI-02 之后，CodeX 二审 #2）**：本单元在 **UI-02 结构迁移之后**执行——`(marketing)` 已位于 **`app/[locale]/(marketing)/`**。下文为可读性仍写 `app/(marketing)/...`，**一律理解为迁移后的 `app/[locale]/(marketing)/...`**；Step 0 必须先以 `[locale]` 真实路径重新核对所有 file 锚点与 grep 目标（`(marketing)` 不再在 `app/` 顶层）。

## 子单元拆分（UI-03a / b / c / d — 可独立成 PR）

| 子单元 | 范围（对应下文步骤） | 依赖 | 人审 | 备注 |
| --- | --- | --- | --- | --- |
| **UI-03a** | 结构化文案抽取（Step 3a.1–3a.4：FAQ / 对比表 / NAV / footer / company-info / SEO 串） | UI-01/02 | 从宽（营销 prose） | 最干净 beachhead |
| **UI-03b** | 内联重文案（Step 3b.1–3b.3：hero / pricing 链路 / trial） | UI-03a | 从宽 | ICU 数字串、保 ¥ |
| **UI-03c** | **legal 人审**（Step 3b.4：terms/privacy/refund/contact + legal-page shell） | UI-01/02 | **HARD 人审** | **独立 PR，不阻塞 3a/3b/3d**；可与 3a/3b 并行，人审慢也不卡营销上线 |
| **UI-03d** | EN 排版 + SEO 翻旗（Step 3c.1–3c.6：typography / 每页 generateMetadata 翻旗 / sitemap·robots / JSON-LD / 布局 QA） | **UI-03a + UI-03b 合并后**（翻旗需 en 内容已在） | — | legal(3c) 可滞后：hreflang 是 **per-page**，legal 页未译就先不挂 en hreflang，不阻塞其余页翻旗 |

> ship-unit 可逐个吃 UI-03a→b→d，3c 并行人审；下文「执行步骤」的 3a/3b/3c 子阶段标题即对应这些子单元（**legal = Step 3b.4 = 子单元 UI-03c**，**子阶段 3c = 子单元 UI-03d**）。

## 元数据

| 项 | 值 |
| --- | --- |
| **目标** | 把 `(marketing)` route group 全量英文化（zh/en 双语 message catalog），打开营销层 SEO 本地化管线（hreflang / canonical / OG / JSON-LD / sitemap / robots 翻旗含 en），加 EN 排版轨道，并在 4 个断点做布局 QA。 |
| **价值** | 最高获客价值：英文长视频创作者可被 Google 双语分别索引、看到英文落地页。Phase 1 与产品 target_language PR#38 无文件重叠，可独立推进。 |
| **关联** | 主方案 **Phase 1 / Task 1.1 + 1.2 + 1.3**；相关 §1.7（英文排版轨道）、§1.8（SEO 本地化）、§0.3 红线 4（SEO anti-leak）/ 红线 5（content 不译）、§1.6（货币 ¥ 真值）、§1.4（ICU）。 |
| **前置依赖** | **UI-02**（`[locale]` 路由 + proxy + `<html lang>` 必须已落地、`/` 与 `/en` 可渲染）；**UI-01**（message catalog 骨架 + `lib/seo/site.ts` 的 per-locale + `absoluteUrl(path, locale)` + hreflang-map helper 必须已建好且 inert）。**两者未合并前不得开工。** |
| **建议分支** | `uiloc/marketing-en-seo`（独立 worktree，显式 pathspec，禁 `git add .`） |
| **预估工时** | **L**（营销 7 页 + 11 个 marketing 组件 + SEO 管线翻旗 + 法务人审 + 4 断点 QA） |

---

## 不在本单元范围（out-of-scope）

- **`app/auth` 全部**（登录/注册/forgot/captcha）→ 归 **UI-04**（Phase 1.5）。营销 CTA 落到 `/auth*` 仍是中文，这是 UI-04 的事，本单元不碰。
- **`(app)` 工作台 / `admin`**（workspace / projects / voices / settings / billing / 中央字典 `types/jobs.ts`·`presentation.ts`）→ Phase 2，不碰。
- **基础设施层**：装 next-intl、`i18n/` helpers、`[locale]` 迁移、proxy 合并、`<html lang>` SSR、`messages/{zh,en}/*.json` namespace 加载机制（固定 namespace import+merge）、CLAUDE.md 约定变更、CJK-lint baseline 守卫脚手架 → 全是 UI-01/UI-02 的事。本单元**只新增** `marketing`/`seo` namespace 的键值与翻旗，**不改** routing/proxy/request 配置。
- **server-emitted 中文**（gateway `detail` / 邮件 / 客服 / 公告）→ Phase 4 后端轨，本单元无法本地化。
- **币种换算 / USD 定价显示**（§1.6）→ Paddle MoR 后续。营销页保留 `¥`/CNY 真值。
- **产品 target_language / 配音方向字段**（§0.1）→ multilingual-mutual-translation 工作，绝不触碰。
- **非 marketing 的 SEO 串**：`site-json-ld.tsx` 的 Organization/WebSite/WebApp 实体 schema 属营销层（home 挂载），**在本单元范围内**；但 app/auth 页面**绝不**加 hreflang/alternate（红线 7）。

## 必守不变量

> 仅列与本单元相关的红线（母方案 §0.3 + 相关条款）。任何步骤违反即 BLOCK。

1. **zh 默认逐字节一致（红线 1）**：交付后默认中文渲染（DOM + URL + SEO 标签 + JSON-LD)必须与改造前逐字节相同。未迁移串以 zh 为 fallback 正常显示。**必须有自动化字节一致回归**（见测试计划）。
2. **SEO anti-leak（红线 4，母方案 §1.8 / GEO §7.4）**：`alternates.canonical` / `alternates.languages`(hreflang) / `openGraph.title|description|locale` **永不**放进 root layout 或共享 layout——只在**每个 page 级** `generateMetadata` 设置。否则泄漏进 `/workspace`/`/admin` 误标记。
3. **content 永不翻译（红线 5）**：FAQ/对比表/legal 是营销 chrome，可译；但 voice 名、`display_title_zh`、说话人名、job 标题、转录/译文、剪映/产物文本一律 verbatim 透传，本单元不接触它们。
4. **hreflang 互惠 + 自指 + 恰好一个 x-default（母方案 §1.8）**：每页 `alternates.languages` = `{ 'zh-Hans': zhUrl, 'en': enUrl, 'x-default': zhUrl }`；en 页 canonical 指**自己**（绝不指 zh，否则自我 de-index）；x-default 指 zh 主市场。
5. **FAQ 可见 Q/A 与 `faq-json-ld` per-locale 字节对齐（母方案 §1.8）**：同一 locale 下，可见 DOM 的 Q/A 文本与 FAQPage JSON-LD 的 `name`/`text` 必须**逐字节相同**（Google 结构化数据硬要求）。两者必须读**同一字典**。
6. **货币 ¥/CNY 真值不动（红线 3 + §1.6）**：数字来自 gateway `price_cny_fen`，locale 切换**只**翻标签/单位（`/ 月` 等）；`¥` 符号保留；**不做币种换算**，不改数字真值。
7. **presentation-only，不碰付费 API（红线 3）**：纯表现层，不新增/改变任何 TTS/clone/LLM/ASR 付费路径，不读写 pipeline 语言字段（`source_language`/`target_language`/`language_pair`/`cn_text`）。
8. **不按检测语言自动重定向（红线 8）**：本单元只翻内容 + 打开 en URL；不引入 `localeDetection:true` 或自动跳转逻辑（那是 UI-02 已定死的 `localeDetection:false`）。
9. **数字内插串必须 ICU（红线 R5 / §1.4）**：`planBenefits`、`leadParagraph`、pricing 单位后缀、trial 描述等含 `${...}` 的串一律走 ICU message，**禁止字符串拼接**（否则英文复数/语序破）。
10. **保持 `siteUrl` 与 gateway `SITE_URL` 同源（site.ts §6.4 契约）**：本单元加 locale 维度时不改 `siteUrl` 的真值来源。

---

## 执行步骤

> **行号会漂移**（多 agent 仓库）。Step 0 必须先重新核对所有 file:line。命令默认 **Git Bash / CI Linux**；PowerShell 等价标注（`grep`→`Select-String`，`test -f`→`Test-Path`，`rg`→`rg`/`Select-String`）。每步用**显式 pathspec** commit。

### Step 0 — 确认现状（必做，先于任何编辑）

- **动作**：重新核对本单元所有目标文件仍存在、UI-01/UI-02 前置已就位、行号锚点。
- **路径变量（避免手滑，CodeX 二审 #4）**：UI-02 后 `(marketing)` 已在 `[locale]/` 下。先在 shell 定义并在后续命令里**用变量替换** `app/(marketing)`：
  ```bash
  MKT="frontend-next/src/app/[locale]/(marketing)"   # 下文凡 app/(marketing) 一律读作 $MKT
  ```
  PowerShell：`$MKT="frontend-next/src/app/[locale]/(marketing)"`。下文验收命令里出现的 `frontend-next/src/app/(marketing)/...` 均以 `$MKT/...` 执行。
- **涉及文件**（核对路径，**不要**信任本文档行号，自行 re-verify）：
  - 营销页：`frontend-next/src/app/(marketing)/page.tsx`、`pricing/page.tsx`、`trial/page.tsx`、`contact/page.tsx`、`terms/page.tsx`、`privacy/page.tsx`、`refund/page.tsx`、`(marketing)/layout.tsx`
  - 组件：`frontend-next/src/components/marketing/{faq,tool-comparison,hero,pricing-grid,site-header,site-footer,legal-page,trial-banner}.tsx`、`company-info.ts`
  - SEO：`frontend-next/src/components/seo/{site-json-ld,faq-json-ld,breadcrumb-json-ld}.tsx`、`frontend-next/src/lib/seo/site.ts`、`frontend-next/src/app/sitemap.ts`、`frontend-next/src/app/robots.ts`
  - 排版：`frontend-next/src/app/globals.css`（ink typography 段，约 551–664 行，含 `.ink-display`/`.ink-heading`/`.marketing-root .ink-heading`/`.zh-body`/`.zh-body-lg`）
  - catalog：`frontend-next/messages/{zh,en}/marketing.json`、`seo.json`（UI-01 建好的骨架）
- **具体改法**：仅核对，不改动。确认 UI-01 的 `lib/seo/site.ts` 已有 `absoluteUrl(path, locale?)` + hreflang-map helper（当前 main 的 `absoluteUrl(path)` 只接 1 参，若仍是单参说明 UI-01 未合 → 停工）。确认 UI-02 的 `[locale]` 路由已生效（`/en` 可渲染）。
- **该步验收**（machine-verifiable）：
  ```bash
  # 前置依赖 sanity：site.ts 必须已有 locale 维度 helper（UI-01 产物）
  rg -n "absoluteUrl\s*\(\s*path\s*:\s*string\s*,\s*locale" frontend-next/src/lib/seo/site.ts
  # marketing/seo 字典骨架存在（UI-01 产物）
  test -f frontend-next/messages/zh/marketing.json && test -f frontend-next/messages/en/marketing.json
  test -f frontend-next/messages/zh/seo.json && test -f frontend-next/messages/en/seo.json
  # 所有目标页存在
  for f in page pricing/page trial/page contact/page terms/page privacy/page refund/page; do test -f "frontend-next/src/app/(marketing)/$f.tsx" || echo "MISSING $f"; done
  ```
  > PowerShell: `Select-String -Path ... -Pattern ...`；`Test-Path ...`。
  > 若任一前置缺失 → **停工**，回报编排层（依赖 UI-01/UI-02 未满足）。

---

## 子阶段 3a — 结构化文案抽取（已是 const 的最干净 beachhead）

> 对应母方案 **Task 1.1**。只迁「已经是 const 数组/对象」的文案——风险最低、机械化、无 JSX 重排。

### Step 3a.1 — 抽取 FAQ（`faq.tsx` GENERAL_FAQ / PRICING_FAQ）→ 字典，保 JSON-LD 字节对齐

- **动作**：把 `GENERAL_FAQ` / `PRICING_FAQ` 的 `{q,a}` 文案迁进 `messages/{zh,en}/marketing.json` 的 `faq` namespace，组件改用 `getTranslations`（Server Component）读取；**可见 Q/A 与传给 `FaqJsonLd` 的 items 必须是同一字典源**。
- **涉及文件**：`components/marketing/faq.tsx`、`components/seo/faq-json-ld.tsx`（仅消费方，不改逻辑）、`messages/{zh,en}/marketing.json`
- **具体改法**：
  - `faq.tsx` 当前是 Server Component（无 `"use client"`），可直接 `const t = await getTranslations('marketing.faq')`。把 GENERAL/PRICING 两组 Q/A 抽成有序数组 key（如 `general[0..7].{q,a}`、`pricing[0..2].{q,a}`）。`PRICING_FAQ = [...GENERAL_FAQ, ...3 条]` 的拼接关系在字典层保留（pricing 引用 general + 追加）。
  - **关键**：构造 `items` 数组后**同时**喂给可见 DOM 与 `<FaqJsonLd items={items} />`——保证不变量 5。不要让 JSON-LD 读 zh、DOM 读 en。
  - 顶部 marquee 提示串（`常见问题`/`你可能想知道`/`鼠标悬停可暂停...`/`定价页`/`还有疑问？...客服...`）也进字典。
  - en 文案：FAQ 属营销 prose，可 MT 辅助 + 人审（非法务，人审从宽）。
- **该步验收**：
  ```bash
  # faq.tsx 不再有内联 CJK 的 q/a（marquee 提示也迁走）
  rg -n "[一-鿿]" frontend-next/src/components/marketing/faq.tsx || echo "no inline CJK left (OK)"
  # 字典含 faq 键
  rg -n '"faq"' frontend-next/messages/zh/marketing.json frontend-next/messages/en/marketing.json
  cd frontend-next && npx tsc --noEmit && npm run lint
  ```
  + **专项测试**（见测试计划）：FAQ JSON-LD ↔ 可见 DOM per-locale 字节对齐测试绿。

### Step 3a.2 — 抽取对比表（`tool-comparison.tsx` ROWS）+ 标题串

- **动作**：`ROWS`（4 行 `{dimension,oneClick,workbench}`）+ section 标题/副标题/表头（`对比维度`/`一键生成工具`/`爱译视频工作台` 等）迁字典。
- **涉及文件**：`components/marketing/tool-comparison.tsx`、`messages/{zh,en}/marketing.json`
- **具体改法**：`tool-comparison.tsx` 是 Server Component，`getTranslations('marketing.comparison')`。ROWS 用有序数组 key（`rows[0..3].{dimension,oneClick,workbench}`）。品牌词 `爱译视频` 在表头属 chrome 可保留中文或用品牌词 key（en 下保留 `爱译视频 AITrans.Video` 双写，遵 site.ts `brandNames`）。
- **该步验收**：
  ```bash
  rg -n "[一-鿿]" frontend-next/src/components/marketing/tool-comparison.tsx || echo "no inline CJK left (OK)"
  cd frontend-next && npx tsc --noEmit && npm run lint
  ```

### Step 3a.3 — 抽取导航/页脚/公司信息（NAV_ITEMS / footer labels / company-info）

- **动作**：`site-header.tsx` 的 `NAV_ITEMS` label（`首页`/`定价`/`免费试用`）、CTA 串（`进入工作台`/`登录`/`试用`/`免费开始试用`）、aria-label（`主导航`/`AITrans.Video 首页`）；`site-footer.tsx` 的标语 + 栏目标题（`产品`/`法律与合规`/`支持`）+ 链接 label + 版权行；`company-info.ts` 的 `PAYMENT_CHANNEL_NOTE` / `DIGITAL_DELIVERABLES`（`COMPANY_NAME`/邮箱是**身份字段，content，不译**）。
- **涉及文件**：`components/marketing/site-header.tsx`、`site-footer.tsx`、`company-info.ts`、`messages/{zh,en}/marketing.json`
- **具体改法**：
  - `site-header.tsx` 是 `"use client"` → 用 `useTranslations('marketing.nav')`（client hook）。NAV_ITEMS 的 `href` 不变（导航 locale 由 UI-02 的 `navigation` Link 处理），只把 `label` 改成 `t(key)`。注意 `href` 仍用 `next/link` 还是 UI-02 的 `@/i18n/navigation` Link——**遵循 UI-02 已落地的约定**，本单元不改导航机制。
  - `site-footer.tsx` 是 Server Component → `getTranslations`。
  - `company-info.ts` 是纯 `.ts` 常量模块（无 React 上下文），**不能**直接 `t()`。`PAYMENT_CHANNEL_NOTE`/`DIGITAL_DELIVERABLES` 改为在**消费点**（`legal-page.tsx ContactBlock` / 各 legal 页）用 `t()` 取，或保留常量但在渲染处覆盖。**`COMPANY_NAME`/`SUPPORT_EMAIL` 不动**（身份/content）。
  - 版权行 `© {year} 爱译视频 AITrans.Video · 长视频翻译配音工作台` 的尾注 chrome 部分进字典；品牌词 + year 保留。
- **该步验收**：
  ```bash
  # NAV label / footer 栏目标题已字典化（品牌词/邮箱/COMPANY_NAME 允许残留）
  rg -n "label: \"首页\"|label: \"定价\"" frontend-next/src/components/marketing/site-header.tsx && echo "STILL INLINE (FAIL)" || echo "NAV migrated (OK)"
  cd frontend-next && npx tsc --noEmit && npm run lint
  ```

### Step 3a.4 — 抽取 SEO 串（`lib/seo/site.ts` defaultTitle/Description/keywords/品牌后缀）→ seo namespace

- **动作**：`site.ts` 的 `siteName`/`defaultTitle`/`defaultDescription`/品牌后缀做 per-locale record（UI-01 已加 locale 维度骨架的话，这里填 en 值）；`site-json-ld.tsx` 的 `inLanguage`/`availableLanguage` 改 locale 驱动。
- **涉及文件**：`lib/seo/site.ts`、`components/seo/site-json-ld.tsx`、`messages/{zh,en}/seo.json`
- **具体改法**：
  - `site.ts`：`siteName`/`defaultTitle`/`defaultDescription` 从单值改为 `{ zh, en }` record（或读 seo.json）。**`siteUrl` 真值来源不动**（不变量 10）。`brandNames` 不变（双写品牌实体）。
  - `site-json-ld.tsx`：当前硬编码 `inLanguage: "zh-CN"`（website + webapp 两处）+ `availableLanguage: ["zh-CN"]` + `contactPoint.availableLanguage:["zh-CN"]`。改为 locale 驱动：zh→`zh-CN`、en→`en-US`；`availableLanguage` 双语 `["zh-CN","en-US"]`。`description` 用 per-locale `defaultDescription`。
  - **守红线 2/4**：`SiteJsonLd` 只在 home 挂载（保持），不上移 layout。
- **该步验收**：
  ```bash
  # site-json-ld 不再硬编码裸 zh-CN（应改为 locale 参数驱动）
  rg -n 'inLanguage: "zh-CN"' frontend-next/src/components/seo/site-json-ld.tsx && echo "STILL HARDCODED (FAIL)" || echo "locale-driven (OK)"
  cd frontend-next && npx tsc --noEmit && npm run lint
  ```

---

## 子阶段 3b — 内联重文案 + 法务人审（JSX 密集页）

> 对应母方案 **Task 1.2**。处理 inline-JSX 重的页面，ICU 化数字内插串，legal 全程**人审**（非纯 MT）。

### Step 3b.1 — hero.tsx（含手动 `<br>` 换行）

- **动作**：hero 的所有可见串迁字典；处理手动 `<br className="hidden sm:block" />` 在英文下的换行策略。
- **涉及文件**：`components/marketing/hero.tsx`、`messages/{zh,en}/marketing.json`
- **具体改法**：
  - 串：eyebrow（`爱译视频 · AITrans.Video`，品牌词保留双写）、h1（`让世界视频，<br>开口说中文`）、lead（`把英文长视频变成可发布的中文配音版。免注册先预览...`）、CTA 旁链接（`查看套餐价格`）、信任行（`免注册试用 · 英文转中文 · 失败不计费 · 支持长视频`）、播放器提示（`鼠标移到画面上自动播放...开启声音...英文原片 / 中文配音 对比`）。
  - **手动 `<br>` 处理**：中文 h1 的 `让世界视频，<br>开口说中文` 在英文里（如 "Make the world's videos speak Chinese"）换行点完全不同。**不要**把 `<br>` 写进 message 字符串。方案：用 next-intl 的 rich-text（`t.rich`）传 `br: () => <br className="hidden sm:block" />` chunk，让每个 locale 的 message 自己决定换行位置（zh 在逗号后断、en 可能不断或别处断）。en message 若不需要 `<br>` 就不放该 tag。
  - hero 是 Server Component（无 `"use client"`），用 `getTranslations`。
- **该步验收**：
  ```bash
  rg -n "[一-鿿]" frontend-next/src/components/marketing/hero.tsx || echo "no inline CJK left (OK)"
  cd frontend-next && npx tsc --noEmit && npm run lint
  ```

### Step 3b.2 — pricing 链路（`(marketing)/pricing/page.tsx` + `pricing-grid.tsx` + `trial-banner.tsx`），ICU 化数字串，保 ¥

- **动作**：pricing 页头文案、`pricing-grid.tsx` 的 `planBenefits`/单位/卡片副标、`trial-banner.tsx` 描述全部字典化；所有数字内插串走 **ICU**；货币标签/单位翻译但 `¥` 与数字真值不动。
- **涉及文件**：`app/(marketing)/pricing/page.tsx`、`components/marketing/pricing-grid.tsx`、`components/marketing/trial-banner.tsx`、`messages/{zh,en}/marketing.json`
- **具体改法**：
  - `pricing/page.tsx`：`PAGE_DESCRIPTION`、h1（`长视频也用得起的 AI 翻译配音`）、lead、eyebrow 迁字典。metadata 改 `generateMetadata`（见 3c）。`BreadcrumbJsonLd` 的 `name`（`首页`/`定价`）per-locale（见 3c.4）。
  - `pricing-grid.tsx`（Server Component）：
    - `monthlyPriceLabel` 的 `unit: "/ 月"`/`"/ 季"`/`"/ 年"` → ICU 或 per-locale key；`formatYuan` 的 `¥` **保留不动**（不变量 6）。
    - `planBenefits` 的全部模板串 ICU 化：`单次视频最长 {n} 分钟`、`最多 {n} 个任务并行处理`、`每月 {credits} 点处理额度（约 {expMin} 分钟 Express / {studioMin} 分钟 Studio 标准）`、`每月 {credits} 点处理额度`、`{quota} 条免费任务额度` 等。`Express 快速模式`/`Studio 精校模式（支持人工复核）`/qualitative fallback/卡片副标（`适合个人创作者试水...` 等）也迁。
    - 空态串 `套餐信息暂时无法加载，请稍后重试。`、`最受欢迎` badge、`免费`。
    - **数字真值全部来自 gateway `data`，不动**；只翻 label。
  - `trial-banner.tsx`（Server Component）：`description` 两分支（hasNumbers / fallback）ICU 化（`注册即享 {days} 天试用，含 {minutes} 分钟源视频额度{studio}`），`免费试用`/`无需绑卡，先体验再决定`/`查看试用说明` 迁字典。
- **该步验收**：
  ```bash
  rg -n "[一-鿿]" frontend-next/src/components/marketing/pricing-grid.tsx frontend-next/src/components/marketing/trial-banner.tsx || echo "no inline CJK (OK)"
  # ¥ 仍在 formatYuan（货币真值未动）
  rg -n '¥' frontend-next/src/components/marketing/pricing-grid.tsx
  cd frontend-next && npx tsc --noEmit && npm run lint
  ```

### Step 3b.3 — trial 页（`(marketing)/trial/page.tsx`），ICU 化 leadParagraph

- **动作**：trial 页全部可见串迁字典，`leadParagraph` 两分支 ICU 化。
- **涉及文件**：`app/(marketing)/trial/page.tsx`、`messages/{zh,en}/marketing.json`
- **具体改法**：`PAGE_DESCRIPTION`、`无需绑卡` badge、h1（`先免费体验，再决定是否升级`）、`leadParagraph`（`注册即享 {days} 天试用，含 {minutes} 分钟源视频额度{studio}...` — ICU）、侧栏（`立即开始`/`创建账户即可...不超过一分钟`/`先看看定价`/三条 `· 无需绑定支付方式` 等）。页面是 `async` Server Component → `getTranslations`。`trial.days`/`source_minutes` 数字来自 gateway，不动。
- **该步验收**：
  ```bash
  rg -n "[一-鿿]" frontend-next/src/app/(marketing)/trial/page.tsx || echo "no inline CJK (OK)"
  cd frontend-next && npx tsc --noEmit && npm run lint
  ```

### Step 3b.4 — legal 页（terms/privacy/refund/contact + `legal-page.tsx`）— **法务人审，非 MT**（= 子单元 **UI-03c**，独立 PR，不阻塞 3a/3b/3d）

- **动作**：4 个 legal 页正文 + `legal-page.tsx` shell（`LegalPage`/`LegalSection`/`LegalClauseList`/`ContactBlock`）的 chrome 串字典化；legal 正文 en 翻译**必须人审**（HARD 要求）。
- **涉及文件**：`app/(marketing)/{terms,privacy,refund,contact}/page.tsx`、`components/marketing/legal-page.tsx`、`messages/{zh,en}/marketing.json`（建议 legal 独立子 namespace `marketing.legal.*`）
- **具体改法**：
  - `legal-page.tsx` shell chrome：`最后更新：`/`生效日期：` 前缀、`运营主体信息`/`主体名称`/`联系邮箱`/`联系地址` label、`PAYMENT_CHANNEL_NOTE`、`（{n}）` 序号格式（ICU 或保留全角括号）。`titleEn` 装饰副标现已是英文，en locale 下应升级为真翻译标题或移除冗余。
  - 4 页正文密集条款（contact 7 节、terms/privacy/refund 各多节）逐节迁字典。**正文翻译挂「人审」标记**——en JSON 值在合入前必须经法务/项目主 review，不得用纯机翻直接上线（consent/退款/隐私措辞有法律效力）。
  - `COMPANY_NAME`/`SUPPORT_EMAIL`/`COMPANY_ADDRESS` 是身份字段，**不译**（content）。
  - **法务人审产出物**：在 PR 描述里附「legal en 译文已人审」勾选 + reviewer 署名；未人审不得合并。
- **该步验收**：
  ```bash
  for p in terms privacy refund contact; do rg -n "[一-鿿]" "frontend-next/src/app/(marketing)/$p/page.tsx" >/dev/null && echo "$p: still has inline CJK (check allowlist)" || echo "$p: clean"; done
  rg -n "[一-鿿]" frontend-next/src/components/marketing/legal-page.tsx || echo "legal-page shell clean (OK)"
  cd frontend-next && npx tsc --noEmit && npm run lint
  ```
  + **人审 gate**（review-verifiable）：PR body 含 legal en 人审署名。

---

## 子阶段 3c（= 子单元 UI-03d）— EN 排版轨道 + SEO 翻旗 + 布局 QA

> 对应母方案 **Task 1.3**。加英文排版、把 inert 的 SEO 管线打开含 en、4 断点布局 QA。

### Step 3c.1 — EN 排版轨道（`globals.css` + next/font 拉丁展示衬线）

- **动作**：在 `globals.css` 加 EN 排版轨道：locale=en 时 `.ink-display` 经 next/font 载一款拉丁展示衬线（Noto Serif SC 只含 CJK、且未经 next/font 打包，英文展示字符回退到 Georgia 不够分量）；加 `.en-body` 收紧 line-height/letter-spacing（`.zh-body` 的 1.75 行高 + 0.005em 字距是为 CJK 调的，英文会显松）。
- **涉及文件**：`app/globals.css`、字体加载点（`app/[locale]/layout.tsx` 由 UI-02 拥有——**与 UI-02 协调**：本单元只**新增** font 变量 + CSS class，若需在 layout 注入 `next/font` 的 `--font-*` 变量，按 UI-02 已建立的字体 `<link>`/变量约定 append，不重排 layout）、可能 `(marketing)/layout.tsx`（挂 `.en-body`/locale class）
- **具体改法**：
  - next/font：复用现有 `--font-eb-garamond`（`.marketing-root .ink-heading` 已用它做拉丁标题）。`.ink-display`（weight 900）当前注释明确说 **EB Garamond caps at 800 会 faux-bold 故排除**——所以 EN `.ink-display` 需另选一款支持 900 或可接受 800 的拉丁展示衬线（如 next/font 载 `Fraunces`/`Newsreader` 等可变字重衬线），新增 `--font-en-display` 变量。
  - CSS：加 locale-scoped 轨道。建议 marketing root 在 en 下挂一个类（如 `.marketing-root[lang="en"]` 或 UI-02 提供的 locale class），定义：
    - `.ink-display` 在 en → `font-family: var(--font-en-display), Georgia, serif`（避开 Noto Serif SC CJK-only 回退）。
    - `.en-body` → 收紧 `line-height: 1.6; letter-spacing: 0`（对照 `.zh-body` 1.75/0.005em）。
  - 注意 Tailwind v4 compound selector quirk（globals.css 现有注释提到 `.marketing-root[data-theme="ink"]` 被 build 剥离、需 `:not(.__never__)` 提 specificity）——en 轨道选择器若用 compound 形式需同样规避，**实施时实跑确认 CSS 生效**。
- **该步验收**：
  ```bash
  rg -n "en-body|font-en-display|--font-en-display" frontend-next/src/app/globals.css
  cd frontend-next && npm run build   # next/font 注入 + CSS 编译必须过
  ```
  + 视觉 QA：en home/pricing 的 h1 不再 faux-bold、英文正文行距收紧（截图见 3c.5）。

### Step 3c.2 — 营销页 metadata 翻旗（per-page `generateMetadata`：self-canonical + hreflang + OG locale）

- **动作**：每个营销页把静态 `metadata` 改为 `generateMetadata({params})`，按 locale 设自指 canonical + 互惠 hreflang languages map（**打开含 en**）+ OG locale。
- **涉及文件**：`app/(marketing)/{page,pricing/page,trial/page,contact/page,terms/page,privacy/page,refund/page}.tsx`、`lib/seo/site.ts`（hreflang-map helper）
- **具体改法**：
  - 每页 `export async function generateMetadata({ params }): Promise<Metadata>`，从 `params.locale` 驱动：
    - `alternates.canonical` = `absoluteUrl(path, locale)`（**本 locale 自指**，en 页指 `/en/...`）。
    - `alternates.languages` = `{ 'zh-Hans': absoluteUrl(path,'zh'), 'en': absoluteUrl(path,'en'), 'x-default': absoluteUrl(path,'zh') }`（不变量 4：互惠 + 自指 + 恰好一个 x-default 指 zh）。用 UI-01 的 hreflang-map helper 生成，**不要**手抄每页。
    - 本地化 `title`/`description`/`openGraph.{title,description}`（读 seo/marketing 字典）；`openGraph.url` = 本 locale URL；`openGraph.locale` = `zh_CN`|`en_US`。
    - **守红线 2/4**：这些**只在 page 级**，绝不进 layout。`og:locale:alternate` Next 16.2 metadata 不支持（母方案 §1.8），可选 JSX 手写 `<meta>`，但优先把 hreflang 做对；本单元默认**不手写** alternateLocale（边际价值低）。
  - home `page.tsx` 当前 `metadata.alternates.canonical: "/"` + 静态 OG → 改 `generateMetadata`。
  - pricing/trial/contact 等 `title: "定价"`（靠 root template 加品牌后缀）——en 下 title 模板的品牌后缀也要 per-locale（与 UI-02 的 layout title template 协调；本单元只改 page 级 title 值，模板后缀归 layout，若 UI-02 已 locale 化则只填 en title 值）。
- **该步验收**：
  ```bash
  # 每页都已切到 generateMetadata（不再纯静态 metadata 导出）
  for f in page pricing/page trial/page contact/page terms/page privacy/page refund/page; do rg -n "generateMetadata" "frontend-next/src/app/(marketing)/$f.tsx" >/dev/null && echo "$f: OK" || echo "$f: MISSING generateMetadata"; done
  cd frontend-next && npx tsc --noEmit && npm run build
  ```
  + **专项测试**：hreflang 互惠/x-default/self-canonical 校验测试绿（见测试计划）。

### Step 3c.3 — sitemap / robots 翻旗含 en

- **动作**：`sitemap.ts` 每条 `publicRoutes` 发 zh/en 互链 `alternates.languages`（Next 自动产 `xhtml:link`）；`robots.ts` 的 `blockedRoutes` disallow 覆盖 `/en` 前缀。
- **涉及文件**：`app/sitemap.ts`、`app/robots.ts`、`lib/seo/site.ts`（`publicRoutes`/`blockedRoutes` + helper）
- **具体改法**：
  - `sitemap.ts`：每条 public route 产出 zh + en 两条（或一条带 `alternates.languages` map）。保留「不写 lastmod」决策（不用 `new Date()`）。**单一 root sitemap**——母方案 Task 0.3 已定**不要求** `/en/sitemap.xml`，本单元只在现有 `/sitemap.xml` 里加 alternates，**不新建** `app/[locale]/sitemap.ts`。
  - `robots.ts`：`blockedRoutes`（`/workspace`/`/admin`/`/settings`...）的 disallow 同时覆盖 `/en/workspace` 等。在 helper 里对每条 blocked route 同时输出裸路径 + `/en` 前缀变体（或 disallow `*/workspace` 模式——按 robots 前缀匹配语义实测）。**红线 7**：app/auth 仍 noindex，不给 hreflang。
- **该步验收**：
  ```bash
  cd frontend-next && npm run build
  # 构建产物或单测确认 sitemap 含 en alternates、robots 覆盖 /en 前缀
  rg -n "alternates|languages" frontend-next/src/app/sitemap.ts
  rg -n "/en" frontend-next/src/app/robots.ts || echo "check robots covers /en prefix"
  ```
  + **专项测试**：sitemap 含 zh/en alternates 测试绿。

### Step 3c.4 — JSON-LD 翻旗（breadcrumb name per-locale + FAQ 双语字节对齐复核）

- **动作**：`breadcrumb-json-ld` 的 `name`（`首页`/`定价`/`免费试用`/`联系我们` 等）改 per-locale；复核 FAQ JSON-LD 在 en 下与可见 DOM 字节对齐（3a.1 已建源，此处验 en 链路）。
- **涉及文件**：调用 `BreadcrumbJsonLd` 的页（`pricing/page.tsx`、`trial/page.tsx`、各 legal 页）、`components/seo/breadcrumb-json-ld.tsx`（仅消费 name，不改逻辑）
- **具体改法**：各页传给 `BreadcrumbJsonLd` 的 `items[].name` 改从字典取（`t('seo.breadcrumb.home')` 等），与可见面包屑/导航 label 同源。`absoluteUrl(path)` 的 `item` URL 用 locale 版本（en 面包屑指 `/en/...`）。FAQ：确认 3a.1 的同源已覆盖 en（同字典喂 DOM + JSON-LD）。
- **该步验收**：
  ```bash
  rg -n '"首页"|"定价"|"免费试用"' frontend-next/src/app/(marketing)/pricing/page.tsx frontend-next/src/app/(marketing)/trial/page.tsx && echo "STILL INLINE breadcrumb name (FAIL)" || echo "breadcrumb name dictized (OK)"
  cd frontend-next && npx tsc --noEmit
  ```
  + FAQ 字节对齐测试在 zh **和** en 双 locale 均绿。

### Step 3c.5 — 布局 QA（320/768/1024/1440）+ Search Console hreflang 互惠校验

- **动作**：在 4 个断点核对英文长文案（普遍比中文长 1.5–2×）不破布局：hero `<br>`、header CTA、tool-comparison 网格、pricing 卡片 overflow/wrap；并校验 hreflang 互惠/x-default/self-reference。
- **涉及文件**：无代码强制改动（QA 步）；若发现 overflow 回改对应组件 CSS（`hero.tsx`/`site-header.tsx`/`tool-comparison.tsx`/`pricing-grid.tsx`/`globals.css`）。
- **具体改法**：
  - 本地起 `npm run dev`，访问 `/en`、`/en/pricing`、`/en/trial`、`/en/contact` 等，在 320/768/1024/1440 截图（可用 `browse`/Playwright skill 或 Chrome MCP）。重点：en hero h1 换行、header 两个 CTA（`登录`/`免费开始试用` 的 en 文案更长）、4 行对比网格、3 列 pricing 卡片 benefit 长行。
  - hreflang 校验：构建后用 Search Console URL 检查 / 第三方 hreflang validator 确认互惠（zh↔en 双向）、恰好一个 x-default 指 zh、每页 canonical 自指。生产翻旗前用 staging 验。
- **该步验收**：
  ```bash
  cd frontend-next && npm run build && npm run lint && npx tsc --noEmit
  ```
  + **QA 产出物**（review-verifiable）：4 断点 × 关键 en 页截图无 overflow，附 PR；hreflang validator 报告互惠通过。

### Step 3c.6 — CJK-lint baseline 同步 + CLAUDE.md 字面无关核对

- **动作**：本单元迁完的串从 UI-01/UI-02 建立的 CJK-lint baseline 快照中**移除**（baseline 只减不增，母方案 §5.1）；确认 marketing 文件不再触发新增内联 CJK 报红。
- **涉及文件**：CJK-lint baseline 快照文件（UI-01/UI-02 产物，路径以其落地为准，常见 `tools/cjk_baseline.json` 之类）、共享 CI/pre-commit（**append 不覆盖**，复用 TU-03 「读 base-ref 基线 / 只阻断新增」模式，§0.6）
- **具体改法**：
  - 把本单元已字典化的 marketing/seo 文件的 baseline 条目删掉（这些文件现在应 CJK-clean，除 allowlist 的品牌词/`COMPANY_NAME`/邮箱）。
  - **不改** CI/pre-commit 的守卫逻辑本身（那是 UI-01/UI-02 + TU-03 的脚手架）；动共享配置前先 `git fetch origin main` + rebase 最新 main，append 自己的 allowlist 条目，绝不覆盖 TU 的 ruff/file-size-guard 块。
- **该步验收**：
  ```bash
  # marketing/seo 文件不再有未登记的内联 CJK（baseline 已移除其条目 → 若残留会报红）
  cd frontend-next && npm run lint   # 含 CJK 守卫的话
  # 共享配置只 append 不覆盖：diff 不应删除 TU-03 的 ruff/file-size-guard 块
  git diff origin/main -- .github/workflows/ci.yml .pre-commit-config.yaml | rg -n "^\-" | rg -i "ruff|file-size|mypy" && echo "WARNING: removed TU block (FAIL)" || echo "no TU block removed (OK)"
  ```

---

## 测试计划

> 新增测试 + 回归。**必含默认 zh 字节一致回归**（红线 1）。**`frontend-next` 无 JS 测试运行器**（无 vitest/jest，CI = `npm run lint` + `npx tsc --noEmit` + `npm run build`）——专项断言一律用**独立 node 脚本 + `npm run` script**，复用 UI-01 立的 `uiloc:key-parity` / `uiloc:cjk-guard` / `uiloc:zh-snapshot`，并按需新增 `uiloc:hreflang-check` / `uiloc:faq-jsonld-parity`（append，不引入重型 runner）。

**新增：**

1. **FAQ JSON-LD ↔ 可见 DOM per-locale 字节对齐**（不变量 5）：渲染 `Faq variant=home/pricing` 于 zh **和** en，断言传给 `FaqJsonLd` 的 `items[].q/a` 与可见 DOM 文本逐字节相同。两 locale 各一组。
2. **hreflang 互惠 + 自指 + x-default**（不变量 4）：对每个营销页的 `generateMetadata` 输出断言 `alternates.languages` 含 `zh-Hans`/`en`/`x-default`，x-default 恰好一个且指 zh，canonical 自指本 locale。
3. **sitemap 含 zh/en alternates**：断言 `sitemap()` 每条 public route 产出 zh + en 互链（不要求 `/en/sitemap.xml`，母方案 Task 0.3）。
4. **robots 覆盖 `/en` 前缀**：断言 `blockedRoutes` 的 disallow 同时命中 `/workspace` 与 `/en/workspace`。
5. **货币真值未动**：断言 `pricing-grid` 在 en locale 下仍渲染 `¥` + 来自 mock gateway 的原始数字（无币种换算）。
6. **ICU 英文语法**：对 `planBenefits`/`leadParagraph`/`trial` 描述的 en ICU 输出做快照，人审英文复数/语序正确（至少 snapshot test）。

**回归（必须保持绿）：**

7. **默认 zh 字节一致**（红线 1，母方案 §5.2 守卫 2）：home/pricing/trial/legal 在 **zh** locale 渲染 DOM snapshot，与本单元改造前逐字节相同。**这是本单元最关键回归**——任何串迁移导致 zh DOM 改变即 FAIL。
8. **`site.ts` `siteUrl` 同源契约**（不变量 10）：断言 `siteUrl` 真值来源未变（仍读 `NEXT_PUBLIC_SITE_URL` + fallback）。
9. **既有 SEO 守卫**（若仓库已有）：home 仍只挂一次 `SiteJsonLd`；canonical/OG 不出现在 layout（红线 4 守卫，若 UI-02 已建则复用）。
10. **`npm run lint` + `npx tsc --noEmit` + `npm run build`** 全绿（CI `frontend` job）。

---

## 回滚方案

- **commit 边界**：三个子阶段（3a/3b/3c）各自独立 commit，便于按子阶段回滚；如拆子 PR 则每 PR 一个回滚单元。
- **优先 `git revert`**（保留历史，多 agent 仓库不 `reset --hard` 共享分支）。
- **分层回滚顺序**（母方案 §6，消费方依赖底座，逆序回）：
  1. 先回滚 **3c 翻旗**（SEO en URL / sitemap-robots en / 切换器可见性）→ en 内容仍在但不对爬虫暴露、不可见 en URL。
  2. 再回滚 **3b/3a 内容字典** → 营销页回 zh fallback。
  3. **不回滚** UI-01/UI-02 框架（被依赖方，非本单元）。
- **紧急止血（不回滚代码）**：若 en 营销出问题但框架要保留——把 UI-01 hreflang-map helper 的 languages map 收回 zh-only（en 条目 inert），或 routing `locales` 收回 `['zh']`（en 路径回 zh）。本单元产出的 en 字典/排版全部 inert，zh 不受影响。
- **涉及回滚的文件**：本单元 touch 的全部 `(marketing)/**`、`components/marketing/**`、`components/seo/**`、`lib/seo/site.ts`、`app/sitemap.ts`、`app/robots.ts`、`globals.css`、`messages/{zh,en}/{marketing,seo}.json` + CJK baseline 条目。

## 完成定义 (DoD)

- [ ] **3a**：FAQ / tool-comparison / NAV / footer / company-info chrome / SEO 串全部抽进 `messages/{zh,en}/{marketing,seo}.json`；对应组件改用 `getTranslations`/`useTranslations`；`rg "[一-鿿]"` 对这些文件无未登记 CJK 残留（品牌词/邮箱 allowlist 除外）。
- [ ] **3a**：FAQ 可见 Q/A 与 `faq-json-ld` 读**同一字典**，zh+en 双 locale 字节对齐测试绿（不变量 5）。
- [ ] **3b**：hero（含 `<br>` 经 `t.rich` 处理）/ pricing 链路 / trial 页全部 en 化；数字内插串全部 ICU（无字符串拼接，红线 R5）；`¥`/CNY 真值与数字未动（不变量 6）。
- [ ] **3b**：legal（terms/privacy/refund/contact + `legal-page.tsx`）en 译文**经人审**，PR body 含 reviewer 署名（review-verifiable）。
- [ ] **3c**：`globals.css` 有 `.en-body` + `--font-en-display`（next/font 拉丁展示衬线）轨道；`npm run build` 过，en h1 不 faux-bold。
- [ ] **3c**：每个营销页用 page 级 `generateMetadata` 设自指 canonical + 互惠 hreflang（含 en）+ OG locale；canonical/OG **不在** layout（红线 4 守卫绿）。
- [ ] **3c**：sitemap 含 zh/en alternates、robots 覆盖 `/en` 前缀；专项测试绿。
- [ ] **3c**：JSON-LD `inLanguage`/`availableLanguage` locale 驱动（双语）；breadcrumb name per-locale。
- [ ] **3c**：4 断点（320/768/1024/1440）× 关键 en 页截图无 overflow（附 PR）；hreflang validator 互惠/x-default/self 通过。
- [ ] **回归**：默认 **zh** DOM 字节一致测试绿（红线 1）；`siteUrl` 同源契约未破（不变量 10）。
- [ ] **CI**：`frontend` job（`npm run lint` + `npx tsc --noEmit`）绿；共享 CI/pre-commit 只 append 未覆盖 TU-03 块（`git diff origin/main` 验证）。
- [ ] **范围闭合**：未触碰 `app/auth`（UI-04）、`(app)`/`admin`（Phase 2）、任何 pipeline 语言字段或付费 API 路径（红线 2/3/5）。

## 关联

- **主方案**：[`docs/plans/2026-06-25-ui-page-locale-switch-plan.md`](../2026-06-25-ui-page-locale-switch-plan.md) — Phase 1（Task 1.1/1.2/1.3）、§1.7、§1.8、§0.3（红线 4/5）、§1.4（ICU）、§1.6（货币）、§5（回归守卫）、§6（回滚）、§0.6（与 TU 并行协调）。
- **前置依赖单元**：
  - **UI-01**（message catalog 骨架 + `lib/seo/site.ts` per-locale + `absoluteUrl(path,locale)` + hreflang-map helper + CJK-lint baseline 脚手架）。
  - **UI-02**（`[locale]` 路由 + proxy 合并 + `<html lang>` SSR + `@/i18n/navigation` Link + 切换器；`/en` 必须可渲染）。
- **后续重叠提醒**：Phase 2 会动 `TranslationForm.tsx`（与产品 PR#38 重叠，§0.5）/ VoiceSelectionPanel（与 TU-11 重叠）——**不在本单元**，但本单元 `messages/` namespace 切分应为 Phase 2 预留 `marketing.*` 不进 app client bundle（§1.4 性能红线）。
- **共享配置协调**：CI `.github/workflows/ci.yml` + `.pre-commit-config.yaml`（TU-03 已建脚手架）— append 不覆盖，动前 rebase main（§0.6）。
