# UI-01 · i18n 基础设施（next-intl no-routing 底座，零路由结构、zh 逐字节一致）

| 项 | 值 |
| --- | --- |
| **目标** | 在 `frontend-next/` 立起 next-intl v4 的**共享基础设施**（库 + `src/i18n/*` helper + `next.config` 插件 + **`messages/{zh,en}/{common,marketing,auth,seo}.json` namespace 骨架** + `site.ts` per-locale 维度 inert + TS messages 类型 + CJK-lint 守卫 + zh 字节一致快照），让 next-intl 能在 **no-routing 模式**（固定 zh）下被 `getTranslations`/`useTranslations` 使用——**不创建 `app/[locale]`、不动 middleware/proxy、不改任何页面/per-page metadata**。 |
| **价值** | 后续 **UI-02（结构迁移 P0b）**、**UI-03/UI-04（营销 + Auth 英文化）** 的**前置地基**；本单元低风险、可独立合并，不碰 auth 白名单/Caddy/付费 API。 |
| **⚠️ messages 形态（CodeX 二审定稿）** | M1 全线用 **namespace-per-file**：`messages/{zh,en}/{common,marketing,auth,seo}.json`。UI-03 写 `marketing`/`seo`、UI-04 写 `auth`——**与本单元一致**。`request.ts` 用**固定 namespace import + merge**（**非** glob、**非**单文件）。Phase 2 再加 `app` namespace。 |
| **关联** | 主方案 [`docs/plans/2026-06-25-ui-page-locale-switch-plan.md`](../2026-06-25-ui-page-locale-switch-plan.md) **Phase P0a = Task 0.1 + 0.5 + 0.6**；相关 §1.1 / §1.4 / §1.6 / §1.8 / §0.3 / §0.4 / §0.6。 |
| **前置依赖** | 无（depends_on: none）。与代码质量 TU-* 并行，仅共享 `.github/workflows/ci.yml` + `.pre-commit-config.yaml`（**append 不覆盖**，§0.6）。 |
| **建议分支** | `uiloc/i18n-foundation`（独立 worktree，显式 pathspec，禁 `git add .`） |
| **预估工时** | M |

---

## 不在本单元范围（out-of-scope）

显式**不做**（属 P0b / UI-02 / UI-03，本单元做了即越界）：

- ❌ **不创建** `app/[locale]/` 动态段；不迁移 `(marketing)`/`(app)`/`(auth)` 任何 route group。
- ❌ **不动** `src/middleware.ts`，**不**改名为 `proxy.ts`，**不**加 locale 检测/重定向。
- ❌ **不删除/不改** `app/layout.tsx` 的 `<html>/<body>`（删 root layout 是 P0b 的事）。
- ❌ **不编辑**任何 `(marketing)`/`(app)`/`(auth)` 页面，**不改**任何 per-page `generateMetadata`、`hero.tsx`、`pricing` 等可见文案。
- ❌ **不新增** `LocaleSwitcher` UI、**不**改 `site-header.tsx`/app 顶栏。
- ❌ **不翻旗** en：`messages/en/*.json` 只是骨架占位（`common` seed、其余 `{}`）；`site.ts` 的 hreflang/languages map **只含 zh（INERT）**，调用方解析到 zh 即原样输出，**零行为变化**。
- ❌ **不碰** pipeline 语言字段（`source_language`/`target_language`/`language_pair`/`cn_text`）、付费 API、TTS/clone/LLM/ASR 路径。
- ❌ **不**改 `sitemap.ts`/`robots.ts` 的实际输出（site.ts 加 helper 但调用点 zh 不变）。

> 本单元的判定线：**任何用户可见 DOM / URL / SEO 标签必须逐字节不变**；只增加「未被默认 zh 路径调用」的底座代码 + CI 守卫。

---

## 必守不变量

仅列与本单元相关的红线（主方案 §0.3 子集 + §1.2a/§1.5/§0.1 相关条）：

1. **zh 默认逐字节一致**（红线 1）：交付后默认中文渲染（DOM + URL + SEO 标签）与改造前**逐字节相同**。本单元不触发任何 `[locale]` 路由，故 URL/页面 0 改动；新增 helper 必须 inert。
2. **`localeDetection: false` 必写**（§1.2a，对齐红线 8）：`routing.ts` 显式 `localeDetection:false`——即便本单元不接 proxy，也先把这条写死，避免后续单元误用默认 `true` 触发按 cookie/Accept-Language 自动跳转。
3. **唯一 `<html>` 结构不被本单元破坏**（§1.5）：本单元**不创建第二个 layout**、不渲染第二个 `<html>`。结构迁移留给 P0b。
4. **content 永不翻译**（红线 5）：messages 骨架只放 chrome key（如导航/按钮/状态标签的占位），**绝不**收录 job 标题、转录/译文、voice 名、`display_title_zh`、说话人名。CJK 守卫的 allowlist 放行 content/domain 术语。
5. **presentation-only，不碰付费 API**（红线 3）：`next.config.ts` 用 `createNextIntlPlugin` 包裹时**必须保留** `output:'standalone'` + 现有 dev rewrites + no-blanket-rewrite guard（被 `tests/test_phase42_e1_next_config_no_blanket_rewrite.py` 守护）。
6. **SEO anti-leak**（红线 4）：`site.ts` 新增的 per-locale record / hreflang helper **只是纯函数**，本单元不把它们接入 root layout，不产生 canonical/hreflang/OG 泄漏。
7. **UI locale 不碰 pipeline 语言字段**（§0.1 硬不变量）：本单元零接触 pipeline。
8. **`siteUrl` 与 gateway `SITE_URL` 同源不变**（site.ts §6.4 契约）：新增 `absoluteUrl(path, locale?)` 不得改变现有 `absoluteUrl(path)` 的输出（zh/无 locale 入参时**逐字节等价**旧实现）。

---

## 执行步骤

> 命令默认 **Git Bash / CI Linux**；PowerShell 等价已在每步注明（`grep`→`Select-String`、`test -f`→`Test-Path`）。所有 commit 用**显式 pathspec**。
> ⚠️ 本仓库 `.git` 是 **shallow + 多 worktree 共享**：**本地绝不跑 `git fetch --depth=1`**（会把 HEAD graft 进共享 shallow → push 被拒，见 memory `feedback_shallow_repo_fetch_footgun`）。CI runner 是 fresh 全 checkout，TU-03 里的 `git fetch --no-tags --depth=1 origin "$BASE"` 仅在 CI 上下文使用，本步骤的 CJK 守卫**沿用同一 CI-only 模式**。

### Step 0 — 确认现状（必做，先验证 file:line）

**动作**：因仓库多 agent 并行、行号会漂移，先重新核对下列锚点的**当前**位置与内容，再开工。

**涉及文件 / 核对项**：
- `frontend-next/package.json` — 确认无 `next-intl`（当前依赖见快照：`next@16.2.1`、`react@19.2.4`、`next-themes@^0.4.6`；**无任何 i18n 库**）。
- `frontend-next/next.config.ts` — 确认 `output:'standalone'` + `rewrites()` 的 no-blanket-rewrite guard 仍在（当前 `/api/plans` allowlist + 注释引用 `tests/test_phase42_e1_next_config_no_blanket_rewrite.py`）。
- `frontend-next/src/lib/seo/site.ts` — 确认 `siteUrl`/`siteName`/`defaultTitle`/`defaultDescription`/`keywords?`/`absoluteUrl(path)` 签名（当前 `absoluteUrl` 单参）。
- `frontend-next/src/app/sitemap.ts`、`frontend-next/src/app/robots.ts`、`frontend-next/src/components/seo/site-json-ld.tsx` — 确认它们 `import ... from "@/lib/seo/site"` 的符号未变（本单元给 site.ts 加符号但**不改**这三个文件的调用）。
- `CLAUDE.md` — 定位「Key Conventions」节那条 **「所有 UI 文本和沟通用中文」**（快照在第 371 行附近）。
- `.github/workflows/ci.yml` — 定位 `frontend` job（当前含 `npm run lint` + `npx tsc --noEmit`）与 TU-03 的 `python-lint`/`file-size-guard` job（「新增阻断 / 改动 report-only / base-ref via FETCH_HEAD」模式）。
- `.pre-commit-config.yaml` — 确认 TU-03 已建（ruff/mypy/通用 hooks），本单元 **append** 一个 local CJK hook。
- 确认 `frontend-next/` **无** vitest/jest 配置（仅 `node_modules` 内有 `*.test.ts`）——**本单元不引入重型测试框架**，守卫/快照用独立 node 脚本 + `npm run` script。

**该步验收**：
```bash
grep -n "next-intl" frontend-next/package.json || echo "OK: next-intl 尚未安装"
grep -n "所有 UI 文本和沟通用中文" CLAUDE.md
test -f frontend-next/next.config.ts && test -f frontend-next/src/lib/seo/site.ts && echo "OK anchors exist"
```
PowerShell 等价：`Select-String -Path frontend-next/package.json -Pattern "next-intl"`；`Test-Path frontend-next/next.config.ts`。

---

### Step 1 — 安装 next-intl@^4

**动作**：在 `frontend-next/` 装 `next-intl@^4`（v4 才有 Next-16/proxy 指南与 cookie 行为，§1.1）。

**涉及文件**：`frontend-next/package.json`、`frontend-next/package-lock.json`

**具体改法**：
```bash
cd frontend-next && npm install next-intl@^4
```
确认 `package.json` `dependencies` 出现 `"next-intl": "^4.x"`，且 `package-lock.json` 同步更新（CI 用 `npm ci`，lockfile 必须提交）。

**该步验收**：
```bash
grep -n '"next-intl"' frontend-next/package.json
cd frontend-next && node -e "require.resolve('next-intl'); console.log('OK resolve next-intl')"
```
PowerShell 等价：`Select-String -Path frontend-next/package.json -Pattern '"next-intl"'`。

---

### Step 2 — 创建 `src/i18n/routing.ts`

**动作**：定义 routing（locales/默认/前缀/`localeDetection:false`/cookie maxAge），但本单元**不**把它接入任何 middleware（仅供 helper 与后续单元引用）。

**涉及文件**：Create `frontend-next/src/i18n/routing.ts`

**具体改法**：
```ts
import { defineRouting } from "next-intl/routing"

export const routing = defineRouting({
  locales: ["zh", "en"],
  defaultLocale: "zh",
  localePrefix: "as-needed",
  // 红线 8 / §1.2a：必须显式 false。默认 true 会按 cookie/Accept-Language 自动重定向。
  localeDetection: false,
  // §1.5：cookie 仅作偏好提示，maxAge 1 年。本单元不写 cookie，仅声明配置。
  localeCookie: { maxAge: 60 * 60 * 24 * 365 },
})
```

**该步验收**：
```bash
test -f frontend-next/src/i18n/routing.ts
grep -n 'localeDetection: false' frontend-next/src/i18n/routing.ts
grep -nE "defaultLocale: \"zh\"|localePrefix: \"as-needed\"" frontend-next/src/i18n/routing.ts
cd frontend-next && npx tsc --noEmit
```
PowerShell 等价：`Select-String frontend-next/src/i18n/routing.ts -Pattern 'localeDetection: false'`。

---

### Step 3 — 创建 `src/i18n/request.ts`（getRequestConfig + hasLocale + 具体 import）

**动作**：`getRequestConfig` 用 `hasLocale` 校验 + **固定 namespace import + merge**（逐个 import `messages/${locale}/{common,marketing,auth,seo}.json` 后按 namespace 合并，**非 glob、非单文件**，§1.4 / CodeX 二审）。

**涉及文件**：Create `frontend-next/src/i18n/request.ts`

**具体改法**：
```ts
import { getRequestConfig } from "next-intl/server"
import { hasLocale } from "next-intl"
import { routing } from "./routing"

// M1 namespace（Phase 2 再加 "app"）。固定列表，便于 client 端 pick 切片（§1.4）。
const NAMESPACES = ["common", "marketing", "auth", "seo"] as const

export default getRequestConfig(async ({ requestLocale }) => {
  const requested = await requestLocale
  const locale = hasLocale(routing.locales, requested)
    ? requested
    : routing.defaultLocale
  // 固定 namespace import + merge（绝不用 glob）。每个文件挂到自己的 namespace 键下，
  // 故 t('marketing.hero.title') 等可正确解析。
  const [common, marketing, auth, seo] = await Promise.all([
    import(`../../messages/${locale}/common.json`),
    import(`../../messages/${locale}/marketing.json`),
    import(`../../messages/${locale}/auth.json`),
    import(`../../messages/${locale}/seo.json`),
  ])
  const messages = {
    common: common.default,
    marketing: marketing.default,
    auth: auth.default,
    seo: seo.default,
  }
  return { locale, messages }
})
```
> 加新 namespace（Phase 2 的 `app`）= 在 `NAMESPACES` + 这两段各加一行；仍是固定 import，无 glob。

**该步验收**：
```bash
test -f frontend-next/src/i18n/request.ts
grep -nE 'messages/\$\{locale\}/(common|marketing|auth|seo)\.json' frontend-next/src/i18n/request.ts && echo "OK 固定 namespace import"
# 反向守卫：确认未用 glob import（如 import(.../*.json)）
! grep -nE 'import\(.*\*' frontend-next/src/i18n/request.ts && echo "OK 无 glob"
```
PowerShell 等价：`Select-String frontend-next/src/i18n/request.ts -Pattern 'messages/\$\{locale\}'`。

---

### Step 4 — 创建 `src/i18n/navigation.ts`

**动作**：`createNavigation(routing)` 导出本地化 `Link/redirect/usePathname/useRouter/getPathname`，供后续单元用（本单元**不**替换任何现有 `<Link>`）。

**涉及文件**：Create `frontend-next/src/i18n/navigation.ts`

**具体改法**：
```ts
import { createNavigation } from "next-intl/navigation"
import { routing } from "./routing"

export const { Link, redirect, usePathname, useRouter, getPathname } =
  createNavigation(routing)
```

**该步验收**：
```bash
test -f frontend-next/src/i18n/navigation.ts
cd frontend-next && npx tsc --noEmit
```

---

### Step 5 — 用 `createNextIntlPlugin` 包裹 `next.config.ts`（保留所有现有约束）

**动作**：把 `next.config.ts` 的导出用 `createNextIntlPlugin('./src/i18n/request.ts')` 包裹，**保留** `output:'standalone'`、现有 `rewrites()`、no-blanket-rewrite guard 注释与逻辑。

**涉及文件**：Modify `frontend-next/next.config.ts`

**具体改法**（最小侵入，仅改导出行）：
```ts
import createNextIntlPlugin from "next-intl/plugin"
// ...（保留现有 nextConfig 定义，含 output:'standalone' + rewrites guard，逐字节不动）...

const withNextIntl = createNextIntlPlugin("./src/i18n/request.ts")
export default withNextIntl(nextConfig)
```
> ⚠️ 红线 5：**不得**删改 `rewrites()` 里的 `/api/plans` allowlist 或 no-blanket-rewrite 注释——`tests/test_phase42_e1_next_config_no_blanket_rewrite.py` 是静态守卫，碰坏即红。

**该步验收**：
```bash
grep -n 'createNextIntlPlugin' frontend-next/next.config.ts
grep -n "output: \"standalone\"" frontend-next/next.config.ts
grep -n "/api/plans" frontend-next/next.config.ts && echo "OK rewrites guard 保留"
cd frontend-next && npm run build   # standalone 产出成功 = 插件包裹未破坏构建
# 后端静态守卫（从 repo root，Linux/Win 通用）
python -m pytest -q tests/test_phase42_e1_next_config_no_blanket_rewrite.py
```
PowerShell 等价：`Select-String frontend-next/next.config.ts -Pattern 'createNextIntlPlugin'`。

---

### Step 6 — `messages/{zh,en}/{common,marketing,auth,seo}.json` namespace 骨架 + key-parity 脚本

**动作**：建 **namespace 文件**（每 locale 4 个：`common`/`marketing`/`auth`/`seo`），`common` seed 几个 chrome key 证明 `getTranslations` 在 no-routing 固定 zh 下跑通；`marketing`/`auth`/`seo` 留 `{}` 占位（UI-03 填 marketing/seo、UI-04 填 auth）。建 `uiloc:key-parity` 脚本（zh↔en 跨全部 namespace 校验，供 UI-03/04 复用）。**不**迁移任何真实页面文案。

**涉及文件**：Create `frontend-next/messages/{zh,en}/{common,marketing,auth,seo}.json`（8 文件）、`frontend-next/scripts/key-parity.mjs`；Modify `frontend-next/package.json`（加 `"uiloc:key-parity": "node scripts/key-parity.mjs"`）

**具体改法**（key 仅占位证明，**绝不含 content**）：
```json
// messages/zh/common.json
{ "appName": "爱译视频", "actions": { "confirm": "确定", "cancel": "取消" } }
// messages/en/common.json
{ "appName": "AITrans.Video", "actions": { "confirm": "Confirm", "cancel": "Cancel" } }
// messages/zh/marketing.json · messages/en/marketing.json · auth.json · seo.json → 先各 {}（占位，UI-03/04 填）
```
> 每个 namespace 的 zh/en 两文件 **key 结构必须一致**（否则 TS 类型与运行时漂移）。`{}` 占位天然一致。

**`scripts/key-parity.mjs`**（遍历 `messages/zh/*.json` 与 `messages/en/*.json`，逐 namespace 比 key 集合，任一 namespace 漏 key → 非零退出）。

**该步验收**：
```bash
for n in common marketing auth seo; do test -f "frontend-next/messages/zh/$n.json" && test -f "frontend-next/messages/en/$n.json" || echo "MISSING $n"; done
cd frontend-next && npm run uiloc:key-parity   # 全 namespace zh↔en key 一致
```

---

### Step 7 — TS-typed messages（全局 `IntlMessages` from zh json）

**动作**：声明全局 `IntlMessages` 让 `t('key')` 有补全 + 打错 key 编译失败（§1.4 必开）。

**涉及文件**：Create `frontend-next/src/global.d.ts`（或 append 已有全局声明文件，Step 0 确认）；必要时 Modify `frontend-next/tsconfig.json`（确认 `messages/*.json` 在 `include` 内 / `resolveJsonModule:true`）。

**具体改法**：
```ts
// global.d.ts —— 用 zh 各 namespace 文件作 messages 形状真源（与 request.ts 的 merge 结构对齐）
import type common from "../messages/zh/common.json"
import type marketing from "../messages/zh/marketing.json"
import type auth from "../messages/zh/auth.json"
import type seo from "../messages/zh/seo.json"

type Messages = {
  common: typeof common
  marketing: typeof marketing
  auth: typeof auth
  seo: typeof seo
}

declare global {
  interface IntlMessages extends Messages {}
}
export {}
```
> 若 `tsconfig.json` 无 `resolveJsonModule` 或 `messages/` 不在 `include`，补上（确认 `npx tsc --noEmit` 能 import json）。空 `{}` 占位的 namespace（marketing/auth/seo）类型为空对象，UI-03/04 填 key 后类型自动扩展。

**该步验收**：
```bash
test -f frontend-next/src/global.d.ts
cd frontend-next && npx tsc --noEmit   # IntlMessages 解析成功、json import 通过
```

---

### Step 8 — `lib/seo/site.ts` 加 per-locale record + `absoluteUrl(path, locale?)` + hreflang-map helper（全 INERT）

**动作**：给 `site.ts` 加 locale 维度，但 **callers 解析 zh ⇒ INERT**（无行为变化，§1.8「先 inert 后翻旗」）。**不改** `sitemap.ts`/`robots.ts`/`site-json-ld.tsx` 的调用。

**涉及文件**：Modify `frontend-next/src/lib/seo/site.ts`

**具体改法**（要点，实施时按当前文件锚定）：
- 新增 per-locale record：`siteName` / `defaultTitle` / `defaultDescription` / `keywords` / 品牌后缀按 `{ zh, en }` 组织；**导出**新符号，但保留旧的顶层 `siteName`/`defaultTitle`/... 常量**逐字节不变**（= `record.zh`），现有 import 0 改动。
- `absoluteUrl(path, locale?)`：**重载/可选第二参**。`locale` 省略或 `=== 'zh'`（默认）时**输出与旧 `absoluteUrl(path)` 逐字节等价**；`locale==='en'` 时返回 `${siteUrl}/en${path...}`（本单元不调用 en 分支）。
- hreflang-map helper：`hreflangLanguages(path): Record<string,string>` 返回 `{ 'zh-Hans': zhUrl, 'x-default': zhUrl }`——**languages map 先只含 zh（inert）**，en 等 UI-02 翻旗再加。
- **保持** `siteUrl` 同源契约不变（§6.4）。

**该步验收**：
```bash
grep -nE "absoluteUrl\(|hreflangLanguages|export const siteName" frontend-next/src/lib/seo/site.ts
cd frontend-next && npx tsc --noEmit
# INERT 等价性由 uiloc:zh-snapshot 机器校验（见 Step 11 / 测试计划 T1）：absoluteUrl('/pricing') 旧值===新值
cd frontend-next && npm run uiloc:zh-snapshot
```
> 等价性以「zh-snapshot / site.ts 单测」（测试计划 T3/T1）机器校验：`absoluteUrl('/pricing')` 旧值 === 新值；`hreflangLanguages('/')` 仅含 zh-Hans + x-default。

---

### Step 9 — CLAUDE.md 约定变更（放宽「所有 UI 文本用中文」为本地化层规则）

**动作**：把 Key Conventions 那条改写为 §0.4 的本地化层规则（否则字面禁止非中文 UI，会自相矛盾阻塞后续单元）。

**涉及文件**：Modify `CLAUDE.md`

**具体改法**：把
```
- 所有 UI 文本和沟通用中文
```
改为：
```
- 所有 UI 文本经本地化层（next-intl message catalog）产出；**zh-CN 是默认/源 locale**；新 UI 串必须是 message key，**不得**新写内联 CJK 字面量（由回归守卫强制，见界面语言切换方案 §5 / CJK-lint）。沟通/文档默认仍用中文。
```

**该步验收**：
```bash
grep -n "zh-CN 是默认/源 locale" CLAUDE.md
! grep -nx -- "- 所有 UI 文本和沟通用中文" CLAUDE.md && echo "OK 旧约定已替换"
```
PowerShell 等价：`Select-String CLAUDE.md -Pattern 'zh-CN 是默认/源 locale'`。

---

### Step 10 — CJK-lint 回归守卫（AST、baseline-snapshot、append 进 TU-03 脚手架）

**动作**：加「禁止新增内联 CJK 字面量」守卫——**AST** 只扫 JSX text 节点 + 面向用户的字符串字面量，**排除注释/JSDoc**，**baseline-snapshot 只阻断新增未登记**，排除 out-of-scope 目录，放行 content/domain 术语（§5.1）。脚本逻辑放 `frontend-next/`，CI 接线 **append** 到既有 `.github/workflows/ci.yml` + `.pre-commit-config.yaml`（不覆盖），复用 TU-03「读 base-ref 基线 / 只阻断新增」模式（§0.6）。

**涉及文件**：
- Create `frontend-next/scripts/cjk-guard.mjs`（AST 扫描，用 TS compiler API 或 `@babel/parser`；优先复用仓库已有依赖，Step 0 评估；无则用 TS 自带 `typescript` 包的 `ts.createSourceFile`——`typescript` 已是 devDep）。
- Create `frontend-next/scripts/cjk-baseline.json`（既有 CJK 占用快照，按 occurrence 记录；**只减不增**）。
- Modify `frontend-next/package.json`（加 `"uiloc:cjk-guard": "node scripts/cjk-guard.mjs"` script）。
- Modify `.github/workflows/ci.yml`（在 `frontend` job **append** 一个 step，或新增 `frontend-cjk-guard` job，复用 `git fetch --no-tags --depth=1 origin "$BASE"` → `FETCH_HEAD HEAD` 两点 diff 取**改动文件**，只对**新增 CJK occurrence** 阻断）。
- Modify `.pre-commit-config.yaml`（append 一个 `repo: local` 的 CJK hook，opt-in，`files: ^frontend-next/src/.*\.(tsx?|jsx?)$`）。

**守卫规格（写进脚本）**：
- 扫描范围：`frontend-next/src/**/*.{ts,tsx,js,jsx,mjs}`。
- 只命中：JSX text 节点 + 字符串/模板字面量里的 CJK（U+4E00–U+9FFF）；**排除** `CommentRange`/JSDoc。
- **排除目录**：`app/(app)/admin/**`、`app/**/workspace/[jobId]/edit/**`（flagged off）。
- **allowlist**：content/domain 术语（voice 名、`display_title_zh`、品牌词 `爱译视频` 等）放行——allowlist 文件 `scripts/cjk-allowlist.json`。
- baseline：`cjk-baseline.json` 登记既有占用；守卫只对**未登记的新增**报红；迁移完成的串从 baseline 移除（单调递减）。

**该步验收**：
```bash
cd frontend-next && npm run uiloc:cjk-guard            # 现状（含既有 CJK）应通过：全部已登记 baseline
# 负向自测：临时插一行新内联 CJK 应被拒
node -e "require('fs').writeFileSync('src/__cjk_probe.tsx','export const x = <div>新增未登记中文</div>')"
cd frontend-next && (npm run uiloc:cjk-guard && echo "FAIL: 守卫漏报" && exit 1) || echo "OK 守卫拦住新增 CJK"
rm -f frontend-next/src/__cjk_probe.tsx
# 排除域自测：admin 下的 CJK 不应触发
grep -n "admin" frontend-next/scripts/cjk-guard.mjs && echo "OK 含 admin 排除"
```
PowerShell 等价：`Remove-Item frontend-next/src/__cjk_probe.tsx`。CI append 校验：`Select-String .github/workflows/ci.yml -Pattern 'cjk-guard'`。

---

### Step 11 — 默认 zh 字节一致快照测试（关键页）

**动作**：对少量关键页（home `(marketing)/page.tsx`、`pricing`、`login`）建「默认 zh 渲染字节一致」快照（§5.2 / DoD P0a）。因 `frontend-next` **无测试框架**，用独立 node 脚本对 **`npm run build` 产物 / SSR HTML** 或对**关键 SEO 纯函数输出**做快照对比，避免引入重型 runner。

**涉及文件**：
- Create `frontend-next/scripts/zh-snapshot.mjs`（生成 + 校验快照）。
- Create `frontend-next/scripts/__snapshots__/zh-baseline.json`（快照基线）。
- Modify `frontend-next/package.json`（加 `"uiloc:zh-snapshot": "node scripts/zh-snapshot.mjs"`）。

**具体改法（务实范围）**：
- 优先快照**纯函数级**不变量（无需起 server）：`absoluteUrl('/')`、`absoluteUrl('/pricing')`、`hreflangLanguages('/')`、`site.ts` 顶层 `siteName`/`defaultTitle`/`defaultDescription` 常量、`messages/zh/common.json` 的 `appName` 等——证明 Step 8 的 site.ts 改造对 zh 调用**逐字节等价**、Step 6 catalog 可读。
- 若实施者愿做更强 DOM 级快照，可在 `npm run build` 后对 home 的 SSG HTML 取 hash 比对（可选增强，非阻塞）。

**该步验收**：
```bash
cd frontend-next && npm run uiloc:zh-snapshot   # 首次写 baseline；再次运行必须全绿
# 负向：改坏 absoluteUrl zh 分支应被快照拦住（手动验证一次后回滚）
```

---

### Step 12 — 全量本地门禁

**动作**：跑齐前端门禁，确认零回归。

**该步验收**：
```bash
cd frontend-next && npm run lint && npx tsc --noEmit && npm run build && npm run uiloc:cjk-guard && npm run uiloc:zh-snapshot
# 后端静态守卫（next.config guard）
python -m pytest -q tests/test_phase42_e1_next_config_no_blanket_rewrite.py
```

---

## 测试计划

### 新增

| ID | 类型 | 内容 | 机器可验证命令 |
| --- | --- | --- | --- |
| T1 | site.ts 单测/快照 | `absoluteUrl(path)` 与 `absoluteUrl(path,'zh')` 输出**逐字节相同**；`hreflangLanguages('/')` 仅含 `zh-Hans` + `x-default`（en INERT 未现身） | `cd frontend-next && npm run uiloc:zh-snapshot` |
| T2 | catalog 可用 | `messages/zh/common.json` 在 no-routing 固定 zh 下能被读取，`appName === "爱译视频"`；各 namespace zh/en **key 结构一致** | `npm run uiloc:key-parity` |
| T3 | CJK 守卫正向 | 现状全部已登记 baseline，`cjk-guard` 通过 | `npm run uiloc:cjk-guard` |
| T4 | CJK 守卫负向 | 临时插入新内联 CJK → 守卫**报红**；admin/edit 目录 CJK **不**触发 | Step 10 负向自测 |
| T5 | 类型门禁 | `IntlMessages` 全局类型解析、json import 通过 | `npx tsc --noEmit` |
| T6 | 构建门禁 | `createNextIntlPlugin` 包裹后 `output:'standalone'` 构建成功 | `npm run build` |

### 回归（必含 default-zh 逐字节一致）

| ID | 内容 | 命令 |
| --- | --- | --- |
| R1 | **默认 zh 字节一致**：关键页/SEO 纯函数输出快照与改造前一致（红线 1） | `npm run uiloc:zh-snapshot` |
| R2 | next.config no-blanket-rewrite guard 未破（红线 5） | `python -m pytest -q tests/test_phase42_e1_next_config_no_blanket_rewrite.py` |
| R3 | `sitemap.ts`/`robots.ts`/`site-json-ld.tsx` 输出不变（本单元未改其调用） | `git diff --stat -- frontend-next/src/app/sitemap.ts frontend-next/src/app/robots.ts frontend-next/src/components/seo/site-json-ld.tsx`（应为空） |
| R4 | lint + 类型零回归 | `npm run lint && npx tsc --noEmit` |
| R5 | CI 共享配置为 **append** 非覆盖：TU-03 既有 job（`python-lint`/`file-size-guard`/`backend*`）仍在 | `git diff -- .github/workflows/ci.yml`（仅净增 CJK 相关行，无删除既有 job） |

---

## 回滚方案

- **提交边界**：本单元在分支 `uiloc/i18n-foundation`，建议拆为可独立 revert 的 commit 粒度——(c1) 装库 + `src/i18n/*` + next.config 包裹；(c2) messages 骨架 + TS 类型；(c3) site.ts inert 维度；(c4) CLAUDE.md 约定；(c5) CJK 守卫 + zh 快照 + CI/pre-commit append。
- **优先 `git revert`**（非 reset）：每个 commit 自包含，逆序 `git revert` 即可下架。
- **紧急止血**：因全部 helper **inert、零页面接线**，最坏只需 revert (c1)（卸 next.config 插件包裹）即恢复改造前构建；messages/site.ts 新符号无人调用，留着也无害。
- **CI 守卫单独可关**：若 CJK 守卫误报阻断他人 PR，先 revert (c5) 的 CI append（保留脚本文件，仅摘 CI job/step），不影响本单元其余产物。
- **不涉及**：DB / 付费 API / 部署 / pipeline——无数据迁移回滚。

---

## 完成定义 (DoD)

- [ ] `next-intl@^4` 已装且 lockfile 提交（`grep '"next-intl"' frontend-next/package.json` 命中；`npm ci` 干净）。
- [ ] `src/i18n/routing.ts` 存在且含 `localeDetection: false` + `defaultLocale:"zh"` + `localePrefix:"as-needed"` + `localeCookie.maxAge=31536000`。
- [ ] `src/i18n/request.ts` 用 `hasLocale` + **固定 namespace import + merge**（`messages/${locale}/{common,marketing,auth,seo}.json`，无 glob，反向 grep 通过）。
- [ ] `src/i18n/navigation.ts` 导出 `Link/redirect/usePathname/useRouter/getPathname`。
- [ ] `next.config.ts` 经 `createNextIntlPlugin` 包裹，**保留** `output:'standalone'` + `/api/plans` rewrites guard；`tests/test_phase42_e1_next_config_no_blanket_rewrite.py` 绿。
- [ ] `messages/{zh,en}/{common,marketing,auth,seo}.json`（8 文件）存在，`common` seed chrome key、其余 `{}` 占位，每 namespace zh/en key 一致（`uiloc:key-parity` 绿），仅含 chrome（无 content）。
- [ ] 全局 `IntlMessages` 类型生效，`npx tsc --noEmit` 通过。
- [ ] `lib/seo/site.ts` 新增 per-locale record + `absoluteUrl(path, locale?)` + `hreflangLanguages` helper，且 **zh/无 locale 调用逐字节等价旧实现**（T1 绿）；`sitemap.ts`/`robots.ts`/`site-json-ld.tsx` **未改**（R3 空 diff）。
- [ ] `CLAUDE.md` Key Conventions 旧条「所有 UI 文本和沟通用中文」已替换为本地化层规则（含「zh-CN 是默认/源 locale」）。
- [ ] CJK-lint 守卫上线：AST、排除注释、baseline-snapshot、排除 admin/edit、放行 content；正向 T3 + 负向 T4 均验证；CI/pre-commit 为 **append**（R5 无删除既有 job）。
- [ ] 默认 zh 字节一致快照（R1）绿；`npm run lint` + `npx tsc --noEmit` + `npm run build` 全绿。
- [ ] **零 URL 改动、零页面文案改动**：未创建 `app/[locale]`、未改 middleware、未改任何 marketing/app/auth 页面（`git diff --stat` 仅触及本单元声明文件）。

---

## 关联

- 主方案：[`docs/plans/2026-06-25-ui-page-locale-switch-plan.md`](../2026-06-25-ui-page-locale-switch-plan.md) — Phase P0a（Task 0.1 / 0.5 / 0.6）；§1.1 库选型、§1.4 消息目录/TS 类型、§1.6 货币格式、§1.8 SEO inert、§0.3 红线、§0.4 约定变更、§0.6 与 TU-* 并行协调、§5 回归守卫。
- 与本单元正交、勿混：[`docs/plans/2026-04-15-i18n-target-language-direction.md`](../2026-04-15-i18n-target-language-direction.md)（配音目标语 `target_language` 轴，§0.1 硬区分）。
- 共享 CI 脚手架来源：`docs/plans/code-quality-tasks/TU-03-quality-scaffold.md`（「只阻断新增 / 读 base-ref 基线」模式，§0.6）。
- **下游依赖本单元**：**UI-02** P0b 结构迁移（`app/[locale]` + proxy 合并 + 删 root layout）、**UI-03** 营销层 EN+SEO 翻旗、**UI-04** 最小 Auth（均消费 site.ts per-locale record / hreflang helper / messages catalog）。
- 多 agent 协作约束：[`CLAUDE.md`](../../../CLAUDE.md) Git 协作模型（独立 worktree + feature 分支 + 显式 pathspec）。
