# UI-02 · 路由结构迁移 + proxy 合并 + 语言切换器（P0b 结构地基）

> **执行框架：** 本仓库跑 ship-unit 单元循环。逐 Step 用 checkbox（`- [ ]`）追踪。命令默认 **Git Bash / CI Linux**；Windows 本机用 PowerShell 等价（见各步注明）。在**独立 worktree + feature 分支**干活，显式 pathspec 提交，**禁** `git add .`（避免误纳 `.codegraph/` / `.codex_worktrees/`）。

| 项 | 值 |
| --- | --- |
| **目标** | 把 `app/(marketing\|app\|auth)/**` 物理迁入 `app/[locale]/` 动态段，删除顶层 `app/layout.tsx` 并让 `app/[locale]/layout.tsx` 成为**本地化主子树的 root layout**（承载其 `<html>/<body>`；独立路由 `paddle-checkout/layout.tsx` 与 `global-*` 壳各自含 `<html>`，属 Next 合法多 root layout）；把 `middleware.ts` 改名并合并为 `proxy.ts`（canonical→locale→auth 三段顺序）；新增语言切换器；SEO special files（`sitemap.ts`/`robots.ts`）升级为 locale-aware（仍 inert，zh-only 内容）。**纯结构 + 切换器 + proxy，不翻任何文案。** |
| **价值** | SEO-correct `/en` 营销的**不可延后地基**：`[locale]` 段必须包住全部 route group，`/en/*` 才能被 Google 分别索引。本单元落地后，`/` 与 `/en/*` 都能渲染（en 以 zh fallback 显示，内容未译），翻译工作量留给 UI-03/UI-04。 |
| **关联** | 主方案 [`2026-06-25-ui-page-locale-switch-plan.md`](../2026-06-25-ui-page-locale-switch-plan.md) **Phase P0b = Task 0.2 + 0.3 + 0.4**；相关 §1.2 / §1.2a / §1.3 / §1.5 / §0.3 / §2 / §8（R1·R2·R3·R10）|
| **前置依赖** | **UI-01**（P0a：装 `next-intl@^4`、`src/i18n/routing.ts`·`request.ts`(固定 namespace import+merge)·`navigation.ts`、`messages/{zh,en}/{common,marketing,auth,seo}.json` namespace 骨架、`next.config.ts` 接 `createNextIntlPlugin`、CJK-lint baseline、CLAUDE.md 约定变更）。**UI-02 假定 `src/i18n/*` 与 next-intl plugin 已就位。** |
| **建议分支** | `uiloc/locale-routing-migration` |
| **预估工时** | **L**（结构迁移 churn 大 + proxy 三段编排 + Next 16.2.1 实测 global-error/global-not-found，最大单点风险，spike 先行）|

---

## 不在本单元范围（out-of-scope）

- **翻译任何文案**：UI-02 **不**抽串、**不**写英文。所有页面继续经 next-intl fallback 渲染中文。营销/auth/app 的实际本地化是 **UI-03 / UI-04**（主方案 Phase 1 / 1.5 / 2 / 3）。
- **SEO 翻旗**：`sitemap.ts`/`robots.ts`/metadata 的 `languages` map 只铺**管线结构**且**只含 zh**（inert）；打开 en alternates / 让 `/en` 进 sitemap 是 UI-03（Phase 1 Task 1.3 翻旗）。本单元 robots 仅**补 `/en` 前缀 disallow 覆盖**（防 en 下 app 路由对爬虫暴露）。
- **per-page canonical / hreflang / OG**：保持在各 page 的 `generateMetadata`，**绝不**进 `[locale]/layout`（红线 4 anti-leak）。本单元不动 page 级 metadata 内容。
- **EN 排版轨道**（`.en-*` / 拉丁衬线）：UI-03（Phase 1 Task 1.3）。
- **App 中央字典 / `JOB_STATUS_LABELS` / `presentation.ts`**：UI-05（Phase 2）。
- **Admin（`app/[locale]/(app)/admin/**`）/ post-edit 子树**：永不翻；本单元只随 route group 整体迁移其物理位置，不动其内容。
- **付费 API / pipeline 语言字段**：完全不碰（见不变量）。
- **`paddle-checkout` 是否进 `[locale]`**：本单元保持其**现状物理位置不动**（见 Step 7 决策点 + §9 开放问题 5）；不擅自迁入 `[locale]`。

---

## 必守不变量（本单元相关红线）

1. **zh 默认逐字节一致（红线 1）**：迁移后默认中文路径 `/`、`/pricing`、`/workspace/...` 的 **URL 不变、DOM 不变、SEO 标签不变**。`localePrefix: 'as-needed'` + `defaultLocale: 'zh'` 保证 zh 永远裸路径；任何 `/zh/...` 自动 301 回 `/...`。
2. **`localeDetection: false`（红线 8 / §1.2a）**：URL 是**唯一**语言真源。裸 `/` **永不**因 `NEXT_LOCALE` cookie 或 `Accept-Language` 头被自动重定向到 `/en`。cookie 只是切换器的偏好提示，**不驱动** `/` 自动恢复。美国爬虫 / 英文浏览器访问 `/` 必须确定性拿到中文。
3. **本地化子树的 `<html>` 单一来源（§1.5 / R3）**：本地化子树（`app/[locale]/**`）的 `<html lang>` **只在** `app/[locale]/layout.tsx` 由 `params.locale` 在 **SSR** 渲染（zh→`zh-Hans`、en→`en`）。**绝不**在 `[locale]` 子树内渲染第二个 `<html>`，**绝不**学 `app-shell.tsx` 用 client effect 盖 `lang`（SSR/hydration 不一致 + 伤 SEO）。顶层 `app/layout.tsx` **删除**（不是 `return children`——那是无效 root layout）。**合法例外（不算违规）**：`[locale]` 之外的独立路由 `app/paddle-checkout/layout.tsx` 与 `global-not-found`/`global-error` 壳**各自是独立 root layout/壳、各自含 `<html>`**——这是 Next 多 root layout 模式，删顶层 layout 后的必需结构（Step 7）。
4. **SEO anti-leak（红线 4 / GEO §7.4）**：`canonical` / `hreflang` / OG title·description **永不**进 `[locale]/layout`。本单元从旧 root layout 迁入 `[locale]/layout` 的只有：`metadataBase`、Search Console verification、字体 `<link>`、`.dark`/theme-color、`SessionProvider`、`Toaster`、skip-to-main、title 模板。`alternates.canonical` 仍只在 page 级。
5. **proxy 三段顺序固定（§1.3 / R2）**：`canonical-origin 重定向(308) → next-intl locale 解析/重定向 → session auth gate`。顺序错会引发重定向环 / 保护页泄漏 / 爬虫被 302 走。matcher 继续排除 `/api`、`/job-api`、`/_next`、静态资源（守住「不拦付费 API 代理路径」）。
6. **不碰 pipeline 语言字段 / 付费 API（红线 2/3）**：本单元纯路由表现层，**不得**读写 `source_language` / `target_language` / `language_pair` / `cn_text`，**不得**新增/触碰任何 TTS/clone/LLM/ASR 路径。content（job 标题、转录、voice 名、`display_title_zh`）一律 verbatim 透传（红线 5）。
7. **`proxy` 仅 nodejs 运行时**：改名前确认现 `middleware.ts` 无 edge-only 依赖（现 auth/canonical 只用普通 cookie/header 读取，nodejs 兼容）。

---

## 执行步骤

> **每步必有该步验收。** 行号会因多 agent 并行漂移——**Step 0 强制重新核对所有 file:line**，下文引用的行号仅为撰写时锚点。

### Step 0 · 确认现状 + SPIKE 先行（最大风险，throwaway 分支验证）

**动作：** (a) 重新核对所有目标文件现状与行号；(b) **先在一次性 throwaway 分支** spike `[locale]` 迁移 + build + 路由矩阵，确认 Next 16.2.1 实际行为，再回主分支做正式迁移。

**涉及文件（核对存在 + 行号漂移）：**
- `frontend-next/src/app/layout.tsx`（撰写时：`<html lang="zh-CN">` @66；verification @52-57；字体 `<link>` @71-76；theme-color @68-70；`SessionProvider` @80；`Toaster` @83；skip-to-main @79；title 模板 @23）
- `frontend-next/src/middleware.ts`（`middleware()` @111；`config.matcher` @158-163；`canonicalRedirect` @73；`publicPaths` @5；`publicExactPaths` @16；auth gate @126-155）
- `frontend-next/src/app/(marketing)/layout.tsx`、`(app)/layout.tsx`、`(auth)/layout.tsx`
- `frontend-next/src/components/app-shell.tsx`（顶栏 @409-451；client `data-theme` effect @150-172——**不动 lang**，保留）
- `frontend-next/src/components/marketing/site-header.tsx`（`NAV_ITEMS` @11；右侧簇 @91-161；内部 `<Link href>` 多处）
- `frontend-next/src/app/sitemap.ts`、`robots.ts`、`lib/seo/site.ts`（`publicRoutes` @45；`blockedRoutes` @74；`absoluteUrl` @89）
- `frontend-next/src/app/paddle-checkout/page.tsx`（独立路由，决策点见 Step 7）
- `frontend-next/next.config.ts`（已由 UI-01 接 `createNextIntlPlugin`——核对存在）
- special files：`app/not-found.tsx`、`app/error.tsx`（**注意：当前是 page 级 error boundary，非 `global-error.tsx`**）、`app/loading.tsx`；`(marketing)/error.tsx`、`(app)/error.tsx`
- 确认 UI-01 产物存在：`src/i18n/routing.ts`、`src/i18n/request.ts`（固定 namespace import+merge）、`src/i18n/navigation.ts`、`messages/{zh,en}/{common,marketing,auth,seo}.json`

**SPIKE（throwaway 分支，做完即弃）：**
- [ ] 从最新 main 切 `uiloc/spike-locale-routing-THROWAWAY`（**不合并**，仅验证）。
- [ ] 最小化把一个 route group（建议 `(marketing)`）迁入 `app/[locale]/`，建一个最小 `[locale]/layout.tsx`（SSR `<html lang>` + `setRequestLocale` + `generateStaticParams`），**删除**顶层 `app/layout.tsx`，跑 `npm run build`。
- [ ] 实测 Next 16.2.1 下：删顶层 layout 后 `[locale]/layout` 是否被接受为 root layout；`global-not-found` / 全局 error 的**确切写法**（是否需要 `app/global-not-found.tsx` / `app/global-error.tsx` 自带 `<html>` 壳，还是 page 级 `not-found.tsx`/`error.tsx` 进 `[locale]/` 即可）。**记录实测结论写进本 Step 注释**，正式迁移照此办。
- [ ] 实测路由矩阵：`/`、`/en`、`/pricing`、`/en/pricing`、`/zh/`（应 301→`/`）能否解析、有无双 `<html>`、有无 hydration mismatch。

**该步验收（machine-verifiable）：**
```bash
# 1. UI-01 产物就位（前置依赖检查）
test -f frontend-next/src/i18n/routing.ts && \
test -f frontend-next/src/i18n/navigation.ts && \
test -f frontend-next/messages/zh/common.json && echo "UI-01 OK"
# 2. spike 分支 build 通过（在 throwaway 分支内）
cd frontend-next && npm run build
# 3. spike 路由矩阵手测后，记录实测结论；删除 throwaway 分支
git branch -D uiloc/spike-locale-routing-THROWAWAY
```
> PowerShell 等价：`test -f X` → `Test-Path X`；`&&` 链 → `; if ($?) { ... }`。

---

### Step 1 · 创建 `app/[locale]/layout.tsx` 作为本地化主子树 root layout

**动作：** 新建 `app/[locale]/layout.tsx`，承载本地化子树的 `<html lang>/<body>` + next-intl 运行时；把旧 root layout 的所有「安全下沉」内容迁入。（独立路由 `paddle-checkout` 的 root layout 见 Step 7。）

**涉及文件：** Create `frontend-next/src/app/[locale]/layout.tsx`

**具体改法：**
- [ ] `"use server"` 默认（RSC）。`export function generateStaticParams()` = `routing.locales.map((locale) => ({ locale }))`。
- [ ] layout 函数签名 `async function LocaleLayout({ children, params })`，`const { locale } = await params`（Next 16 params 为 Promise）；用 `hasLocale(routing.locales, locale)` 校验，不合法 → `notFound()`。
- [ ] 进函数体先 `setRequestLocale(locale)`（来自 `next-intl/server`）——**漏调会静默退化为 dynamic 渲染**（R10）。
- [ ] 渲染**唯一** `<html lang={locale === 'zh' ? 'zh-Hans' : 'en'} className="dark h-full antialiased">` + `<head>`（迁入字体 `<link>` / theme-color / color-scheme，逐字节照搬旧 `app/layout.tsx` @67-77）+ `<body className="min-h-full bg-background">`。
- [ ] `<body>` 内逐字节照搬旧结构：skip-to-main `<a href="#main-content" className="skip-to-main">跳到主内容</a>`（**此中文串本单元保持内联**，UI-03/04 再字典化）→ `<SessionProvider>{...}</SessionProvider>` → `<Toaster position="top-center" richColors />`。
- [ ] 用 `getMessages()` 取 messages，包 `<NextIntlClientProvider messages={pick(messages, [...needed namespaces...])}>` 在 children 外层。**起步可整本下发**（UI-01 messages 为 namespace 文件、M1 体量小）；标注 TODO：bundle 压力出现时用 `pick` 按 route group 收窄（§1.4 client 切片，留给 UI-03/04 细化）。
- [ ] **`export const metadata`**：迁入旧 root 的 `metadataBase`、`title.template`（`"%s · 爱译视频 AITrans.Video"`）、`description`、`keywords`、`openGraph.{siteName,locale,type}`、`verification.{google,other.msvalidate.01}`。**不迁入** `alternates.canonical`（本就不在旧 root，守红线 4）。
> ⚠️ `metadataBase`/verification 在 root layout 合法（GEO §7.4 允许 fall-through 项）；canonical/OG title·description 仍只在 page 级。

**该步验收：**
```bash
test -f "frontend-next/src/app/[locale]/layout.tsx"
# 本地化子树唯一 <html>：排除独立 root（paddle-checkout/layout、global-*）后只剩 [locale]/layout.tsx
grep -rn "<html" frontend-next/src/app | grep -vE "global-|paddle-checkout" ; echo "expect: only [locale]/layout.tsx（paddle-checkout/layout + global-* 是合法独立 root，已排除）"
# setRequestLocale 已调用
grep -n "setRequestLocale" "frontend-next/src/app/[locale]/layout.tsx"
```
> PowerShell：`grep -rn` → `Select-String -Path frontend-next/src/app/**/* -Pattern '<html>'`。

---

### Step 2 · 物理迁移三个 route group + 删除顶层 `app/layout.tsx`

**动作：** 把 `(marketing)`/`(app)`/`(auth)` 整体移入 `app/[locale]/`；删除顶层 `app/layout.tsx`；按 Step 0 实测结论安置 special files。

**涉及文件：** Move `app/(marketing|app|auth)/**` → `app/[locale]/(...)/**`；**Delete** `app/layout.tsx`；安置 `not-found`/`error`/`global-*`（按实测）

**具体改法：**
- [ ] `git mv` 三个 route group 目录到 `app/[locale]/` 下（route group 可嵌套在动态段内）：
  ```bash
  cd frontend-next/src/app
  git mv "(marketing)" "[locale]/(marketing)"
  git mv "(app)"       "[locale]/(app)"
  git mv "(auth)"      "[locale]/(auth)"
  ```
  > PowerShell：`git mv` 同样可用；含括号路径用引号包裹。
- [ ] **删除**顶层 `app/layout.tsx`：`git rm frontend-next/src/app/layout.tsx`（内容已在 Step 1 迁入 `[locale]/layout.tsx`）。
- [ ] **special files 安置（照 Step 0 实测）**：
  - `app/sitemap.ts` / `robots.ts` **留在 `app/` 顶层不动**（无 DOM，仍是 `/sitemap.xml`、`/robots.txt`）——Step 6 改其内容。
  - `app/not-found.tsx` / `app/error.tsx`：root 级 `not-found`/`error` 在删掉顶层 layout 后**缺少 `<html>` 壳**。按实测改为 `app/global-not-found.tsx` / `app/global-error.tsx`（自带 `<html>/<body>` 的 Next 16.2.1 写法），或把它们的 zh 版下移进 `[locale]/`。**保留现有中文文案逐字节**（`找不到页面` / `页面暂时无法打开` 等本单元不翻）。
  - `(marketing)/error.tsx`、`(app)/error.tsx`、各 `loading.tsx` 随 route group 一并迁移（已被 `git mv` 带走）。
- [ ] 全局检查：迁移后无文件再 `import ... from "@/app/layout"`（旧 root 已删）。

**该步验收：**
```bash
test ! -f frontend-next/src/app/layout.tsx && echo "old root deleted"
test -d "frontend-next/src/app/[locale]/(marketing)" && \
test -d "frontend-next/src/app/[locale]/(app)" && \
test -d "frontend-next/src/app/[locale]/(auth)" && echo "groups moved"
test -f frontend-next/src/app/sitemap.ts && test -f frontend-next/src/app/robots.ts && echo "special files stay at app root"
cd frontend-next && npm run build   # 必须绿：root layout 解析正确、无双 <html>
```

---

### Step 3 · 内部导航改用 `src/i18n/navigation`

**动作：** 把所有内部 `<Link href>` / `router.push` / `usePathname` / `redirect` 从 `next/link` · `next/navigation` 切到 UI-01 的 `src/i18n/navigation`（locale-aware，否则导航丢前缀）。

**涉及文件：** Modify 全 `frontend-next/src` 内使用 `next/link`·`next/navigation` 导航的组件（重点：`app-shell.tsx`、`site-header.tsx`、`site-footer.tsx`、各页 `<Link>`、`useRouter().push/replace`）

**具体改法：**
- [ ] `import Link from "next/link"` → `import { Link } from "@/i18n/navigation"`。
- [ ] `import { useRouter, usePathname, redirect } from "next/navigation"` 中的**导航类**（`useRouter`/`usePathname`/`redirect`/`getPathname`）→ `@/i18n/navigation`。**非导航类**（`useSearchParams`、`notFound`）**保留** `next/navigation`（i18n navigation 不导出它们——见 `paddle-checkout` 用的 `useSearchParams`）。
- [ ] `app-shell.tsx`：`Link`（@3）改源；`usePathname`（@4）改源。**保留** client `data-theme` effect（@150-172，不碰 lang）。
- [ ] `site-header.tsx`：`Link`（@4）、`usePathname`（@5）改源。`NAV_ITEMS` 的 `href`（`/`、`/pricing`、`/trial`）**不变**——locale 由 navigation 注入。
- [ ] 外部链接 / `<a href>` 指向后端 API（如 `/auth/logout` POST、`window.location.href`）**保持原生**，不经 i18n navigation（它们不是页面路由）。

**该步验收：**
```bash
# 内部页面 Link 已切源（site-header / app-shell 不再裸 import next/link 作页面导航）
grep -rn 'from "next/link"' frontend-next/src/components frontend-next/src/app ; echo "expect: 0 page-nav hits (剩余仅特殊豁免须注释说明)"
cd frontend-next && npm run build && npm run lint
```
> 漂移说明：若个别文件保留 `next/link`（如纯外链场景），须在该处加注释说明豁免理由。

---

### Step 4 · `middleware.ts` → `proxy.ts` 合并（canonical → locale → auth）

**动作：** 改名并合并三段中间件逻辑为单一 `src/proxy.ts`，严格按 canonical→locale→auth 顺序；白名单 locale-aware（单点 `stripLocalePrefix` 归一化）。

**涉及文件：** Rename `frontend-next/src/middleware.ts` → `frontend-next/src/proxy.ts`；Modify 内容

**具体改法：**
- [ ] `git mv frontend-next/src/middleware.ts frontend-next/src/proxy.ts`。
- [ ] 导出函数 `middleware`（@111）→ `proxy`。`config.matcher`（@158-163）保留并继续排除 `/_next/static`、`/_next/image`、`favicon.ico`、`job-api`、`api/`。
- [ ] 函数体三段顺序（不可错，R2）：
  1. `const redirect = canonicalRedirect(request); if (redirect) return redirect;`（**保持现 308 canonical 逻辑 @73-109 不动**）。
  2. **locale 层**：用 next-intl `createMiddleware(routing)`（来自 `next-intl/middleware`）**手动编排**进函数体——`const intlResponse = intlMiddleware(request);` 若它返回重定向（locale 归一化，如 `/zh/x`→`/x`）则 `return intlResponse`，否则继续。**不要**让 next-intl 作独立中间件与本函数打架。
  3. **auth gate**：保留现 @126-155 逻辑，但**匹配前单点归一化**——`const normalizedPath = stripLocalePrefix(pathname)`（剥 `/en` 前缀），再用 `normalizedPath` 去比 `publicExactPaths`/`publicPaths`。**不要**给每条白名单复制 `/en` 变体。
- [ ] 新增 `stripLocalePrefix(pathname)` helper（剥非默认 locale 前缀；`/en/pricing`→`/pricing`，`/pricing`→`/pricing`），单点使用。
- [ ] 未登录重定向到 `/auth/login`（@149-152）：用 i18n-aware 路径（保留访客所在 locale 前缀，避免英文访客被甩回中文登录）；`from` 参数仍记原 `pathname`。

**该步验收（machine-verifiable + 回归用例）：**
```bash
test -f frontend-next/src/proxy.ts && test ! -f frontend-next/src/middleware.ts && echo "renamed"
grep -n "export function proxy\|export default proxy\|export { proxy }" frontend-next/src/proxy.ts
grep -n "stripLocalePrefix" frontend-next/src/proxy.ts   # 单点归一化存在
cd frontend-next && npm run build
```
**手测回归矩阵（Step 0 spike 工具复用）：** 未登录访问 `/en/workspace` → 302 到登录（保留 en 前缀）；`/sitemap.xml` 200 且无重定向；`/robots.txt` 200；`/` 与 `/en` 无重定向环；`/zh/pricing` → 301 `/pricing`。

---

### Step 5 · 新增 `LocaleSwitcher` 并挂载

**动作：** 新建语言切换器组件，经 **URL 前缀**切换（语言真源是 URL，cookie 仅手动偏好提示，不自动恢复 `/`）；挂到 site-header 右侧簇 + app 顶栏。

**涉及文件：** Create `frontend-next/src/components/i18n/LocaleSwitcher.tsx`；Modify `site-header.tsx`、`app-shell.tsx`（顶栏 @425-450）

**具体改法：**
- [ ] `LocaleSwitcher.tsx`（`"use client"`）：用 `useRouter`/`usePathname` from `@/i18n/navigation` + `useLocale()` from `next-intl`。切换：`router.replace(pathname, { locale: next })` —— next-intl navigation 自动按 `localePrefix:'as-needed'` 产出正确 URL（zh 裸 / en 带 `/en`）。
- [ ] **cookie 偏好（可选）**：切换时可手动写 `NEXT_LOCALE` cookie 作偏好状态，但**不得**驱动 `/` 自动重定向（`localeDetection:false`，红线 8）。**不**实现自动恢复横幅（§1.2a 列为可选增强，**留给 UI-03**，本单元只做开关本身）。
- [ ] UI：镜像 `next-themes` 日/月 toggle 交互风格（中/EN 二态）；a11y——`aria-label`（如 `切换界面语言` / 当前语言标注）、键盘可达、`type="button"`。
- [ ] 挂载：`site-header.tsx` 右侧簇（@91-161，已是 client component）插入；`app-shell.tsx` 顶栏右侧（@425-450）插入。**保持现有按钮/簇布局不破**（4 断点不溢出，见验收）。

**该步验收：**
```bash
test -f frontend-next/src/components/i18n/LocaleSwitcher.tsx
grep -n "LocaleSwitcher" frontend-next/src/components/marketing/site-header.tsx frontend-next/src/components/app-shell.tsx
# 切换器经 URL 前缀（用 i18n navigation router.replace，不靠 cookie 自动恢复）
grep -n "router.replace\|useRouter" frontend-next/src/components/i18n/LocaleSwitcher.tsx
cd frontend-next && npm run build && npm run lint
```
**手测：** header/顶栏点切换 → URL 在 `/pricing` ⇄ `/en/pricing` 间切；裸 `/` 刷新仍中文（不因 cookie 跳 en）。

---

### Step 6 · `sitemap.ts` / `robots.ts` locale-aware（仍 inert，zh-only）+ site.ts locale 维度

**动作：** 给 sitemap 加 locale-aware `alternates` **结构**（`languages` map 先只含 zh，inert）；robots 的 `disallow` 覆盖 `/en` 前缀；`site.ts` 加 `absoluteUrl(path, locale?)` 重载。**不翻旗、不放 en 内容。**

**涉及文件：** Modify `frontend-next/src/app/sitemap.ts`、`robots.ts`、`lib/seo/site.ts`

**具体改法：**
- [ ] `lib/seo/site.ts`：`absoluteUrl(path, locale?)` 增加可选 `locale` 参数（`en` → 前缀 `/en`，`zh`/缺省 → 裸；`/` 特例保留）。**不改** `siteUrl` / `publicRoutes` / `blockedRoutes` 既有值（保 `siteUrl` 与 gateway `SITE_URL` 同源，§6.4 契约）。新增「返回某 path 的 hreflang map」helper（**先只产 zh 自指**，inert）。
- [ ] `sitemap.ts`：每条 `publicRoutes` 加 `alternates: { languages: { 'zh-Hans': absoluteUrl(path, 'zh') } }`（**只含 zh，inert**）；保留「不写 lastmod / changeFrequency / priority」决策（别用 `new Date()` 撒谎）。**en alternates 留给 UI-03 翻旗。**
- [ ] `robots.ts`：`blockedRoutes` 的 disallow 须覆盖 `/en` 前缀变体（如 `/en/workspace`、`/en/admin`…）——否则 en locale 下 app 路由对爬虫暴露。最简：在 `sharedRules.disallow` 里同时铺 `blockedRoutes` 与其 `/en`-prefixed 版本（单点 map 生成，不手抄）。
- [ ] **守 GEO §7.4**：以上只在 special file / page 级，**不进** `[locale]/layout`。

**该步验收：**
```bash
grep -n "alternates" frontend-next/src/app/sitemap.ts          # languages 结构存在
grep -n "/en" frontend-next/src/app/robots.ts                  # en 前缀 disallow 覆盖
grep -n "locale" frontend-next/src/lib/seo/site.ts             # absoluteUrl 加 locale 维度
cd frontend-next && npm run build
# inert 校验：sitemap 当前不得出现 en URL（翻旗在 UI-03）
grep -c "/en" frontend-next/src/app/sitemap.ts ; echo "expect: 0 (sitemap still zh-only)"
```

---

### Step 7 · `paddle-checkout` 独立路由必须自带 root layout（CodeX 二审：本单元当场解决，不留 §9）

> **为什么必须现在解决（CodeX 二审 #3）**：Step 2 删除了顶层 `app/layout.tsx`，`[locale]/layout.tsx` 成为 `[locale]` 子树的 root layout。但 `app/paddle-checkout/page.tsx` 在 `[locale]` **之外**——删掉顶层 layout 后它**没有任何 root layout（缺 `<html>/<body>`）→ 构建/渲染报错**。这不能留作 §9 开放问题，UI-02 必须当场给它一个 root layout。

**决策（采纳）：** 给 `paddle-checkout` 建**独立的 locale-neutral root layout**，保持它在 `[locale]` 之外（Next.js「多 root layout」模式：`app/[locale]/layout.tsx` 与 `app/paddle-checkout/layout.tsx` 各自是一个 root layout，前提是**没有**共享的 `app/layout.tsx`——Step 2 已删）。

**涉及文件：** Create `frontend-next/src/app/paddle-checkout/layout.tsx`（新增 locale-neutral root layout）；`paddle-checkout/page.tsx` 内容不动

**具体改法：**
- [ ] 新建 `app/paddle-checkout/layout.tsx`：渲染自带的 `<html lang="zh-Hans"><body>{children}</body></html>`（locale-neutral，与界面 locale 解耦——它是支付 handoff，硬编码 `locale:'zh-Hans'` 传 Paddle.js @162）。**不**包 `NextIntlClientProvider`（无需 i18n）。最小化即可，但必须含 `<html>/<body>`。
- [ ] **Step 0 SPIKE 必须实测**「`[locale]/layout` + `paddle-checkout/layout` 双 root layout」在 Next 16.2.1 下 build 通过、`/paddle-checkout` 与 `/`/`/en` 都正常——这是删顶层 layout 后的关键验证项。
- [ ] 确认 proxy matcher / auth gate 对 `/paddle-checkout` 行为不变（现 middleware 未在白名单，保持现状，不改其鉴权语义；`stripLocalePrefix` 对无 locale 前缀的 `/paddle-checkout` 原样返回）。
- [ ] **产品问题仍留 §9.2 Q5**：是否进一步把 paddle-checkout 迁入 `[locale]` 并做 zh-Hans/en→Paddle locale 映射，是**产品**决策；但**结构上它现在有自己的 root layout，build 不再受删顶层 layout 影响**。

**该步验收：**
```bash
test -f frontend-next/src/app/paddle-checkout/layout.tsx   # 独立 root layout 存在
grep -n "<html" frontend-next/src/app/paddle-checkout/layout.tsx   # 自带 <html>/<body>
cd frontend-next && npm run build   # 双 root layout 必须 build 绿（关键）
# 路由可达手测：/paddle-checkout?_ptxn=test 200（非 404、非被 locale 段吞掉、非缺 root layout 报错）
```

---

### Step 8 · 全量构建 + 回归门 + 默认 zh 字节一致校验

**动作：** 完整 build + lint + 复用 UI-01 CJK-lint baseline（不新增内联 CJK——本单元只搬不写文案）+ 默认 zh 字节一致回归。

**涉及文件：** 无新改动（验证步）

**该步验收：**
```bash
cd frontend-next && npm run build && npm run lint
# CJK baseline 不增（本单元零新增文案；skip-to-main/error 中文是逐字节搬运，应已在 baseline）
cd frontend-next && npm run uiloc:cjk-guard
# 默认 zh 字节一致（复用 UI-01 的 uiloc:zh-snapshot 机制，本单元把迁移后关键页加入其覆盖清单）
cd frontend-next && npm run uiloc:zh-snapshot
```

---

## 测试计划

### 新增

1. **proxy 三段顺序 + locale 归一化（核心，R2）** — 单元/集成测 `src/proxy.ts`：
   - 未登录 `/en/workspace` → 302 登录（保留 en 前缀）。
   - `/zh/pricing` → 301 `/pricing`（`localePrefix:'as-needed'` 归一化）。
   - 裸 `/` 携 `NEXT_LOCALE=en` cookie + `Accept-Language: en` → **仍 200 渲染中文，不重定向**（`localeDetection:false`，红线 8）。
   - `/sitemap.xml`、`/robots.txt`、`/healthz.txt` → 200 无重定向。
   - canonical 重定向在 locale/auth **之前**生效（顺序断言）。
   - `stripLocalePrefix` 纯函数单测：`/en/x`→`/x`、`/x`→`/x`、`/`→`/`。
2. **本地化子树 `<html lang>` SSR（R3）** — 测 `app/[locale]/layout.tsx` 渲染：`locale=zh`→`lang="zh-Hans"`，`locale=en`→`lang="en"`；本地化子树（`[locale]/**`）只有 `[locale]/layout.tsx` 一处 `<html>`（`paddle-checkout/layout.tsx` + `global-*` 是合法独立 root/壳，各自含 `<html>`，不计入）；无 client effect 写 `lang`。
3. **路由矩阵 build smoke** — `npm run build` 后 `/`、`/en`、`/pricing`、`/en/pricing`、`/workspace/...`、`/en/workspace/...` 均产出页面、无双 `<html>`、无 hydration mismatch。
4. **LocaleSwitcher 行为** — 切换调用 `router.replace(pathname,{locale})`；a11y（aria-label、键盘、`type="button"`）。
5. **sitemap inert 结构** — sitemap 含 `alternates.languages` 结构但**只 zh**（断言无 en URL）；robots disallow 覆盖 `/en` 前缀。

### 回归（必含默认 zh 字节一致）

6. **默认 zh 逐字节一致（红线 1，硬回归）** — 关键页（`/` home / `/pricing` / `/workspace` 详情 / `/auth/login`）**改造前后 SSR DOM snapshot 逐字节比对**：迁移到 `[locale]/` + 删顶层 layout 后，默认 zh 输出（含 `<html lang="zh-Hans">`——注：旧为 `zh-CN`，**此为已知预期差异，须在快照基线显式登记并经项目主确认**，其余 DOM/文案/SEO 标签不变）。**URL 不变**（`/pricing` 仍 `/pricing`）。
7. **现有 auth gate 回归** — 复用/保留现 middleware 行为测：公共路径白名单、静态资源早出、API 路径不拦（守付费 API 代理路径）。
8. **`sanitizedProgressMessages` / content 透传不破** — 主方案守卫 4/5 仍绿（本单元不碰，但 build 必须不回归）。
9. **CJK baseline 不增** — UI-01 守卫：本单元零新增内联 CJK（搬运的 skip-to-main/error 中文已在 baseline）。

> ⚠️ **`<html lang>` `zh-CN`→`zh-Hans` 是有意变更**（§1.5）。这会让红线 1「逐字节」在 `<html>` 标签上产生 1 处预期 diff——**必须在字节一致快照基线里显式登记此单点豁免并经项目主确认**，不得作为「字节一致」被静默掩盖。

---

## 回滚方案

- **提交边界**：本单元在 `uiloc/locale-routing-migration` 分支，**建议拆 3 个原子提交**便于精准回滚：① `[locale]` 迁移 + 删顶层 layout（Step 1/2）；② proxy 合并（Step 4）+ navigation 切换（Step 3）；③ LocaleSwitcher + sitemap/robots（Step 5/6）。
- **优先 `git revert`**（非 `reset --hard`）：保留历史可审计。`git mv` 的目录迁移 revert 会原样搬回，干净。
- **分级回滚**：
  - 仅切换器出问题 → revert 提交③，框架/proxy 留存。
  - proxy 重定向环 / 泄漏 → revert 提交②，回到旧 `middleware.ts`（框架结构在但无 locale 解析——此中间态**不可发布**，仅供本地排障）。
  - 结构性失败 → revert 提交①②③全量回到 main（旧扁平结构 + `middleware.ts`）。
- **紧急止血（合并后线上）**：把 `src/i18n/routing.ts` 的 `locales` 收回 `['zh']`（en 路径 404/重定向回 zh），**保留框架不动**——en/SEO 全 inert，zh 不受影响（主方案 §6 紧急止血）。本回滚改的是 UI-01 文件，须与 UI-01 owner 协调。
- **依赖警示**：本单元是被 UI-03/UI-04 依赖的地基；若它们已基于本单元落地，回滚本单元须先回滚下游。

---

## 完成定义 (DoD)

- [ ] **Step 0 spike** 已在 throwaway 分支验证 build + 路由矩阵 + Next 16.2.1 global-error/global-not-found 写法，结论记录在 Step 0/2 注释；throwaway 分支已删。
- [ ] `app/[locale]/layout.tsx` 是**本地化主子树的 root layout**，承载其唯一 `<html lang>`（SSR、`zh→zh-Hans`/`en→en`）+ `<body>`；`grep "<html"` 排除 `paddle-checkout`/`global-*` 后仅 `[locale]/layout.tsx` 一处。
- [ ] `app/paddle-checkout/layout.tsx` 独立 root layout 存在且自带 `<html>`（Step 7）；`npm run build` 双 root layout 绿。
- [ ] 顶层 `app/layout.tsx` **已删除**（`test ! -f`），其安全内容（metadataBase/verification/字体/theme-color/SessionProvider/Toaster/skip-to-main/title 模板）已迁入 `[locale]/layout`；canonical/OG title·description **未**进该 layout（红线 4）。
- [ ] 三个 route group 已 `git mv` 入 `app/[locale]/`；`sitemap.ts`/`robots.ts` 留顶层；special files 按实测安置。
- [ ] 所有内部页面导航改用 `@/i18n/navigation`（`grep 'from "next/link"'` 仅余注释豁免项）。
- [ ] `middleware.ts`→`proxy.ts`，导出 `proxy`，三段顺序 canonical→locale→auth，单点 `stripLocalePrefix`，matcher 仍排除 `/api`·`/job-api`·`/_next`·静态资源。
- [ ] `proxy` nodejs-only 确认无 edge-only 依赖。
- [ ] `LocaleSwitcher.tsx` 经 URL 前缀切换（`router.replace(pathname,{locale})`），cookie 不自动恢复 `/`，a11y 达标；已挂 site-header + app 顶栏。
- [ ] `sitemap.ts` 有 `alternates.languages` 结构但**只 zh（inert，0 个 en URL）**；`robots.ts` disallow 覆盖 `/en` 前缀；`site.ts` `absoluteUrl(path,locale?)` 加 locale 维度且 `siteUrl`/`publicRoutes`/`blockedRoutes` 既有值不变。
- [ ] `paddle-checkout` 保持顶层不动，路由可达，决策点记录留 §9。
- [ ] `localeDetection:false` 行为实测：裸 `/` 携 en cookie/Accept-Language **不跳转**，确定性渲染中文。
- [ ] `npm run build` + `npm run lint` 全绿；新增 proxy/layout/switcher 测试绿；**默认 zh 字节一致回归绿**（`<html lang` `zh-CN→zh-Hans` 单点豁免已登记并经项目主确认）。
- [ ] CJK baseline 未新增（本单元零新增内联文案）。
- [ ] **未触碰**任何 pipeline 语言字段 / 付费 API / TTS·clone·LLM·ASR 路径（review 确认）。

---

## 关联

- **主方案**：[`docs/plans/2026-06-25-ui-page-locale-switch-plan.md`](../2026-06-25-ui-page-locale-switch-plan.md) — Phase P0b（Task 0.2 / 0.3 / 0.4）；§1.2 / §1.2a / §1.3 / §1.5 / §0.3 / §2 / §8（R1·R2·R3·R10）/ §9。
- **前置依赖单元**：**UI-01**（P0a 非结构基础设施：next-intl 安装、`src/i18n/*` helpers、messages 骨架、`next.config.ts` plugin、CJK-lint baseline、CLAUDE.md 约定变更）。
- **下游依赖本单元**：**UI-03**（Phase 1 营销层 EN + SEO 翻旗 + EN 排版轨道）、**UI-04**（Phase 1.5 最小 Auth EN）；后续 **UI-05**（Phase 2 App 中央字典）、**UI-06**（Phase 2 App 用户流）。
- **正交勿混**：[`docs/plans/2026-04-15-i18n-target-language-direction.md`](../2026-04-15-i18n-target-language-direction.md)（产品配音方向 `target_language` 轴，与本界面语言轴正交，§0.1 硬区分）。
- **并行协调**：代码质量 TU-* 单元（[`code-quality-tasks/TU-00-INDEX.md`](../code-quality-tasks/TU-00-INDEX.md)）——共享 `.github/workflows/ci.yml` / `.pre-commit-config.yaml` 须 **append 不覆盖**，动前 rebase 最新 main，复用 TU-03「只阻断新增 / 读 base-ref 基线」模式（主方案 §0.6）。
