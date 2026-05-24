---
id: T2-msg-003
task: T2
from: Claude-Code
to: CodeX
type: report
status: complete
priority: high
reply_to: T2-msg-002
requires_human: false
created_at: 2026-04-05 19:30 Asia/Shanghai
---

# T2 阶段完成汇报

## 1. 执行范围

本轮执行了 **Task 2: marketing 三页最小闭环正式实施**：

1. **Global foundations 对齐 DESIGN.md** —— 更新 `globals.css` 颜色 token（从紫色移到 deep blue / steel cyan）、中文优先字体栈、marketing 专属 utility class
2. **Marketing 布局重写** —— `(marketing)/layout.tsx` 替换为 `SiteHeader` + `<main>` + `SiteFooter` 三段式，覆盖 T1 的紫青占位 header
3. **首页 `/`** —— Hero → Features → Workflow → Pricing Preview → FAQ → Final CTA 六段结构
4. **定价页 `/pricing`** —— Page header + Trial banner + 三档卡片（Free / Plus / Pro，Plus 高亮） + 定价 FAQ
5. **试用页 `/trial`** —— Hero + 左侧权益说明 + 右侧 CTA 侧边栏，完全遵守 `trial.frozen = false` 边界
6. **共享组件层** —— 在 `components/marketing/` 新建 14 个纯展示组件

**明确没有进入的后续任务：**
- Task 3：手机号登录 / 短信验证码 / captcha gate / Trial 发放逻辑 —— 全部未触碰
- Task 4：subscription 模型 / billing invoice / PaymentOrder 迁移 —— 全部未触碰
- Task 5：真实支付渠道 —— 未触碰
- 未修改 `(auth)/*`、`(app)/*`、`SessionProvider`、`lib/billing/types.ts`、`lib/billing/get-plans.ts`
- 未修改任意 gateway / tests / Alembic / payment provider 文件
- 未新增 pricing tier，未把 Trial 做成第四张套餐卡
- 未锁定任何 Trial 天数 / 分钟数 / Studio 权益 / 价格 / 支付方式口径

## 2. 取数策略决策

### 本轮采用 client fetch 路径

严格遵守 T2 指令 §"默认实施决策"：pricing / trial 页面通过浏览器端读取 `GET /api/plans`，**没有**把 T2 扩展成 SSR / server-safe 取数改造。

### 具体实现

1. **已有 `frontend-next/src/lib/billing/get-plans.ts`**（T0 交付）完全未修改，保留原有 fetch 语义
2. 新建 `components/marketing/use-plans.ts` —— 一个客户端 React hook，包装 `getPlans()` 并维护 `loading / ready / error` 三态
3. 消费点全部是 `"use client"` 组件：`<PricingGrid />`、`<TrialBanner />`、`<TrialDetails />`
4. Server 组件（如 `page.tsx`）不直接调 `getPlans()`，而是把 client 组件作为子节点挂载，避免踩到 SSR fetch 的绝对 URL 陷阱

### 为什么没有进入 SSR 改造

- T2 指令明确要求"默认采用 client fetch 路径"
- SSR 需要重构 `get-plans.ts` 添加绝对 URL 解析（`process.env.NEXT_PUBLIC_GATEWAY_URL` 或 server-only 的内部 host），这超出了 T2 的允许修改范围（`get-plans.ts` 在"不要修改"列表中）
- client fetch 对 marketing 页面完全够用：这些页面允许有一瞬间的加载骨架，且 SEO 不依赖首屏价格数字（价格会变，真相源在 gateway）

## 3. 页面实现结果

### 3.1 首页 `/`

六个 section 按 DESIGN.md §3.2 顺序编排：

| # | Section | 组件 | 说明 |
|---|---------|------|------|
| 1 | Hero | `<Hero />` | Dark-capable media surface（`marketing-hero-surface`），标题"精准对齐的视频翻译，直出剪映草稿"，主 CTA + "查看定价"次 CTA + 信任说明"无需绑卡 · 试用结束不会自动扣费" |
| 2 | Features | `<Features />` | 三栏：DSP 级精准对齐 / 工程化输出 / 增量重生成，在 `marketing-reading-surface` 上呈现（浅/中性背景） |
| 3 | Workflow | `<WorkflowShowcase />` | 四步骤：导入 → 翻译配音 → 人工复核 → 导出，在 `bg-muted/40` 上呈现 |
| 4 | Pricing preview | `<PricingPreview />` | 复用 `<TrialBanner />` + `<PricingGrid />` + "查看完整套餐对比"链接 |
| 5 | FAQ | `<Faq variant="home" />` | 通用 FAQ，4 条问答（不含 Trial 数字） |
| 6 | Final CTA | `<FinalCta />` | Dark-capable surface + 主 CTA + "查看套餐"次 CTA |

### 3.2 `/pricing` 如何消费 `/api/plans`

```
PricingPage (server component, has metadata)
  └── Page header (h1 "简单透明，为实际产出买单")
  └── TrialBanner (client) ──┐
  └── PricingGrid (client) ──┼─ 都通过 usePlans() → getPlans() → /api/plans
  └── Faq variant="pricing" ─┘
```

**数据流：**

1. `usePlans()` hook 在 mount 后发起 `GET /api/plans`
2. 返回 `PlansResponse { plans: Plan[], trial: TrialConfig }`
3. `<PricingGrid />` 按 `PLAN_ORDER = ["free", "plus", "pro"]` 过滤排序（**永远不会出现第四张 Trial 卡**，类型层面保证）
4. 价格通过 `monthlyPriceLabel(plan.price_cny_fen)` 从 fen 转换为"¥X / 月"
5. Gate 字段（`max_duration_minutes` / `max_concurrent_jobs` / `allowed_service_modes` / `free_quota_total`）通过 `planBenefits()` 生成权益列表
6. Plus 卡片有 `highlight = true`，使用 `primary` 变体按钮 + `ring-primary/30` 环形边框 + "最受欢迎" Badge

**无任何硬编码的价格、分钟数、配额、支付承诺文案。** 所有数字字段来自 API 响应。

**三态处理：**
- `loading` → 3 个 skeleton 卡片
- `error` → 中性 "套餐信息暂时无法加载，请稍后重试" 提示
- `ready` → 三张卡片按 free / plus / pro 顺序渲染

### 3.3 `/trial` 如何处理 `trial.frozen = false`

关键组件 `<TrialDetails />`（`components/marketing/trial-details.tsx`）严格遵守边界：

**永远渲染（无论 frozen 状态）：**
- 4 条定性权益：体验完整工作流 / 项目安全保留 / 无需绑卡 / 结束后会怎样?
- 每条都是**纯文字**信任表达，**没有任何数字**

**分支渲染：**
```tsx
{state.status === "loading" && <Skeleton ... />}
{state.status === "ready" && !frozen && (
  <p>试用的具体天数与额度仍在最终确认中，以正式发放时的数据为准。</p>
)}
{state.status === "ready" && frozen && (
  // 仅在 gateway 翻转 frozen = true 且添加 days / source_minutes 字段后生效
  <p>试用权益已根据当前规则发放，详情可在注册后查看。</p>
)}
```

**浏览器验证断言：**
```
bodyHasTrialNumbers: false  // /7\s*天|20\s*分钟|days|minutes/i.test(main.textContent) === false
```

未登录默认 CTA："免费开始试用" → `/auth/register`；次 CTA："先看看定价" → `/pricing`。

**本页不做的事：**
- 不实现手机号登录、短信验证码、captcha gate
- 不实现 Trial 实际发放逻辑
- 不包含任何表单
- 只是 marketing landing page，把访客交给现有 `/auth/register` 流程

## 4. DESIGN.md 对齐说明

### 4.1 已替换的 T1 占位

| T1 占位 | T2 替换 |
|---------|---------|
| `(marketing)/layout.tsx` 的 `from-violet-500 to-cyan-500` 渐变 logo | `<SiteHeader />` 使用新的 `<BrandLockup />`（`bg-primary` 实心小方块 + "AV" 字样，无渐变） |
| `(marketing)/page.tsx` 的占位大渐变图形 + "正在建设中"文案 | 完整的六段首页实现 |
| `globals.css` 中的 "Synthetix Dark (Purple + Cyan)" 主题 | DESIGN.md §2.2 对齐的 deep blue / steel cyan 中性 slate 基座 |
| `globals.css` 底部的 `--color-violet-*` 扩展调色板 | 删除。保留 status colors，其他 marketing token 通过 utility class 收口 |

### 4.2 如何避免默认 AI purple 风格

1. **基础色变更** —— `--primary` 从 `oklch(0.55 0.24 285)`（紫色）改为 `oklch(0.5 0.17 252)`（deep blue），`.dark` 对应从 `oklch(0.65 0.24 285)` 改为 `oklch(0.66 0.15 244)`。chroma 大幅下降（0.24 → 0.15），远离霓虹感。
2. **secondary 保留为 steel cyan** —— 原来的 `oklch(0.65 0.15 195)` 已符合 DESIGN.md "steel cyan"，微调 hue 到 210
3. **背景从纯蓝紫色基座改为 slate** —— 暗色背景从 `oklch(0.08 0.02 260)` 改为 `oklch(0.13 0.015 250)`，更接近 graphite / slate
4. **不再引入任何紫色到青色的渐变** —— `<BrandMark />` 使用 solid `bg-primary`
5. **Hero 的 dark surface 使用 slate 渐变** —— `marketing-hero-surface` 是 `oklch(0.11 0.02 250)` 到 `oklch(0.14 0.02 250)` 的 slate 渐变 + 顶部微弱的 primary radial glow，而不是紫到青的霓虹条带

### 4.3 如何防止 marketing 表达层外溢到 (app) / billing / admin

1. **所有 marketing-specific 样式都以 `marketing-*` 前缀的 utility class 存在：**
   - `.marketing-hero-surface` — dark-capable 深色 surface
   - `.marketing-reading-surface` — 中性阅读 surface
   - `.marketing-divider` — 分隔线
   - `.zh-body` / `.zh-body-lg` — 中文长文本专用行距

   这些 class 只在 `components/marketing/*` 内使用，不会被 `(app)` / billing / admin 意外继承。

2. **所有 marketing 组件物理隔离在 `components/marketing/`** —— 与 `components/ui/`（shared 基础组件）分开，`(app)` 页面不会 import 任何 marketing 组件。

3. **foundations（颜色 / 字体 / 圆角 / 间距）是共享的** —— 这正是 DESIGN.md §4.1 要求的："inherit foundations, not the full marketing expression layer"。`(app)` 的 `AppShell` 在下一次打开时会自动继承新的 deep blue primary，但不会继承 `marketing-hero-surface` 的戏剧性渐变，因为那是专属 class。

4. **没有在 `globals.css` 里注入全局 hero 背景或戏剧化动效** —— 所有 drama 都是 scoped utility class，`(app)` 页面的 `<body>` 仍然是干净的 `bg-background`。

5. **字体栈变更是 foundation 层** —— 加入 `"PingFang SC"`, `"Microsoft YaHei"` 等中文系统字体，对所有层都有益（包括工作台长文本场景），这是 DESIGN.md §2.3 的共享要求。

## 5. 实际修改文件

### 5.1 修改（existing files）

| 文件 | 改动 |
|------|------|
| `frontend-next/src/app/globals.css` | 颜色 token 重写（紫 → deep blue / steel cyan）；字体栈加入中文系统字体；移除 Synthetix violet/cyan 扩展调色板；新增 marketing utility classes（`.marketing-hero-surface`, `.marketing-reading-surface`, `.marketing-divider`, `.zh-body`, `.zh-body-lg`） |
| `frontend-next/src/app/(marketing)/layout.tsx` | 替换为使用 `<SiteHeader />` + `<main>` + `<SiteFooter />` 的三段式 layout，覆盖 T1 的内联紫青 header |
| `frontend-next/src/app/(marketing)/page.tsx` | 替换 T1 的最小占位页，composable 地组合 Hero / Features / Workflow / PricingPreview / Faq / FinalCta 六个 section |
| `frontend-next/src/middleware.ts` | 在 `publicExactPaths` 中加入 `/pricing` 和 `/trial`，让未登录访客可访问 marketing 层页面（与 T1 中 `/` 的处理一致，最小必要改动） |

### 5.2 新建（components/marketing/）

| 文件 | 作用 |
|------|------|
| `brand-mark.tsx` | `<BrandMark />` + `<BrandLockup />`，使用 solid `bg-primary` 的极简品牌标识，取代 T1 的紫青渐变 |
| `site-header.tsx` | Marketing 层粘性 header，包含 logo / 导航（首页/定价/免费试用）/ CTA 按钮（guest: 登录+试用；logged-in: 进入工作台） |
| `site-footer.tsx` | Marketing 层页脚：logo + tagline + footer 导航 + 版权 |
| `primary-cta.tsx` | `<PrimaryCta />` 客户端组件，消费 `useSession()`；guest → 免费开始试用→/auth/register，logged-in → 进入工作台→/translations/new |
| `link-button.tsx` | `<LinkButton />` 客户端 wrapper，把 `next/link` 通过 `buttonVariants()` 渲染成按钮样式。存在原因：本仓库的 base-ui Button 不支持 Slot 式 `asChild`，且 `buttonVariants` 在 "use client" 模块里，server 组件无法直接调用 |
| `use-plans.ts` | `usePlans()` hook，客户端包装 `getPlans()` + `loading/ready/error` 三态 |
| `hero.tsx` | `<Hero />` 首页英雄区，使用 `marketing-hero-surface` 深色 media panel |
| `features.tsx` | `<Features />` 三栏核心价值区（DSP 级对齐 / 工程化输出 / 增量重生成） |
| `workflow-showcase.tsx` | `<WorkflowShowcase />` 四步工作流 |
| `pricing-grid.tsx` | `<PricingGrid />` 从 `/api/plans` 消费三档卡片，Plus 高亮。强约束：按 `["free","plus","pro"]` 顺序过滤，永远不会渲染第四张卡 |
| `trial-banner.tsx` | `<TrialBanner />` Trial 转化入口 banner（非 pricing tier），文案在 `trial.frozen` 未解冻时不含任何数字 |
| `pricing-preview.tsx` | `<PricingPreview />` 首页定价预览段，复用 `<TrialBanner />` + `<PricingGrid />` |
| `faq.tsx` | `<Faq variant="home" \| "pricing" />` FAQ 区，pricing variant 多 2 条（试用结束 + 时长计算） |
| `final-cta.tsx` | `<FinalCta />` 底部 CTA 段，深色 surface |
| `trial-details.tsx` | `<TrialDetails />` 试用页权益列表 + frozen 边界处理（数字字段永远从 API 读，绝不硬编码） |

### 5.3 新建（页面）

| 文件 | 作用 |
|------|------|
| `frontend-next/src/app/(marketing)/pricing/page.tsx` | `/pricing` 页面，server component + metadata |
| `frontend-next/src/app/(marketing)/trial/page.tsx` | `/trial` 页面，server component + metadata |

### 5.4 统计

- 新建文件：**17 个**（15 个 marketing 组件 + 2 个页面）
- 修改文件：**4 个**（globals.css + 2 个 layout/page + middleware.ts）
- 未修改：任何 `(auth)/*`、`(app)/*`、`SessionProvider`、`lib/billing/*`、gateway、测试、migration、payment provider

## 6. 执行命令与验证结果

### 6.1 `npm run lint`

```
✖ 5 problems (0 errors, 5 warnings)
```

**0 errors。** 5 个 warnings 均为 T2 前已存在的 unused vars + custom font 警告，本次未引入任何新 lint 问题。

### 6.2 `npm run build`

```
✓ Compiled successfully in 7.7s
✓ Generating static pages (20/20) in 861ms

Route (app)
┌ ○ /                         ← 新的 marketing 首页
├ ○ /auth/login
├ ○ /auth/register
├ ○ /pricing                  ← 新增
├ ○ /trial                    ← 新增
├ ○ /admin/...
├ ○ /translations/new
├ ○ /tasks/current
├ ○ /projects
├ ƒ /projects/[jobId]
├ ○ /voices
├ ○ /usage
├ ○ /settings
├ ○ /notifications
├ ○ /help
└ ƒ /workspace/[jobId]

ƒ Proxy (Middleware)
```

全部 20 条路由预渲染通过（相比 T1 的 18 条增加了 `/pricing` 和 `/trial`）。

### 6.3 构建过程中遇到并修复的两个问题

1. **stale `.next` cache** —— 首次 build 有 TS 类型缓存残留，`rm -rf .next && npm run build` 后通过
2. **base-ui Button 不支持 `asChild`** —— 首次尝试使用 shadcn 风格的 `<Button asChild><Link /></Button>` 失败，因为仓库的 `components/ui/button.tsx` 基于 `@base-ui/react/button` 而非 Slot。进一步发现 `buttonVariants` 位于 `"use client"` 模块内，server 组件无法直接调用。**解决方案：** 新建 `components/marketing/link-button.tsx`（一个 `"use client"` 的 `<Link>` wrapper），让 server 组件也能复用 button 视觉样式而不复制 class 字符串。所有 CTA 都改为使用 `<LinkButton>` 或 `<Link className={buttonVariants(...)}>`。

### 6.4 浏览器核验结果

| 验收点 | 结果 |
|--------|------|
| `/` 返回 200，渲染 marketing 首页 | ✅ title = "AIVideoTrans"，hero 标题 "精准对齐的视频翻译，直出剪映草稿"，无重定向，无紫青渐变 |
| `/pricing` 返回 200，显示三档套餐 | ✅ h1 = "简单透明，为实际产出买单"；`PricingGrid` 组件按 `["free","plus","pro"]` 顺序渲染，Plus 高亮。**preview 环境下 gateway 未运行，`/api/plans` 404，页面优雅降级显示"套餐信息暂时无法加载"错误态**（证明无硬编码 fallback 数字） |
| `/pricing` 不出现第四张 Trial 套餐卡 | ✅ 代码层面通过 `PLAN_ORDER` 白名单过滤保证；TrialBanner 渲染为独立 banner，不进入卡片网格 |
| `/trial` 返回 200，`trial.frozen = false` 时不显示未冻结数字 | ✅ h1 = "先免费体验，再决定是否升级"；断言 `bodyHasTrialNumbers` 正则 `/7\s*天\|20\s*分钟\|days\|minutes/i` **= false**（main 内无任何 trial 数字 leak） |
| 未登录状态主 CTA 指向 `/auth/register` | ✅ 断言通过：header "免费开始试用" → `/auth/register`；hero `<PrimaryCta>` 渲染为 "免费开始试用" → `/auth/register`；trial 页主 CTA 相同 |
| 控制台 0 errors | ✅ `preview_console_logs level=error` 返回 `No console logs` |

### 6.5 网络层观察（preview 环境限制）

Preview 环境只运行 Next.js dev server，**Python gateway 未启动**，所以：
- `/api/plans` 返回 404（Next.js 本身没有这个路由）
- `/auth/me` 返回 404（同上）

这不是 T2 代码的 bug —— 在生产环境下，gateway 通过 Caddy 反向代理到 FastAPI，`/api/plans` 由 T0 落地的 `plan_catalog.router` 响应。T2 的所有组件在遭遇 404 时都有**优雅降级路径**：
- `PricingGrid` → error 状态 "套餐信息暂时无法加载"
- `TrialBanner` → 保留文字 copy，不渲染任何数字
- `TrialDetails` → 保留定性权益列表
- `SessionProvider` → user 保持 null，guest CTA 正常显示

**代码层面不存在任何把 "gateway 不可用" 变成 "渲染硬编码数字" 的退路。** 这是 T2 的核心约束之一。

## 7. 风险与边界

### 7.1 不属于本轮但仍存在的问题

1. **生产环境端到端的 PricingGrid 渲染未被 preview 验证** —— 因为 preview 没有 Python gateway，三张卡片在 preview 里永远显示 error 状态。我在本地通过代码 review + TypeScript 检查 + build 的静态分析确认了渲染路径正确，但要在真实部署环境才能看到三张实际卡片。**建议 CodeX 在部署环境下访问 `/pricing` 做最后的肉眼确认。**

2. **SSR 未被改造** —— 所有 pricing/trial 的数据消费都是 client-side。这意味着未登录访客首屏会有一次 loading skeleton 闪烁，SEO 爬虫也抓不到具体价格数字。这是 T2 指令明确的默认决策，后续如果需要 SSR 可以在新任务里单独做 `get-plans.ts` 的 server-safe 改造。

3. **marketing 组件目前没有专门的测试** —— 前端测试基础设施在这个仓库中还不完善。lint + build + TypeScript 是当前的静态保障。如果后续需要视觉回归测试（例如 Chromatic / Playwright），应作为独立任务。

4. **Hero / Features / FAQ 的文案是"候选方向"而非"最终锁定"** —— 按 WG3 的降级口径，文案如"精准对齐的视频翻译，直出剪映草稿"、"DSP 级精准对齐"、"工程化输出"等都属于 candidate copy，可能随最终产品定位或 i18n 方案再调。它们不影响任何业务事实，但后续任务可以替换。

5. **真实 demo 素材 / 产品截图尚未接入** —— WorkflowShowcase 目前是四段文字卡片，没有视频 / 截图占位。Task 2 指令说"不要开始营销页 section 正式视觉落地（在 T1 阶段）"，现在 section 骨架已就位，后续可以加入真实 mockup 或 looping demo。

6. **middleware.ts 超出原始允许文件列表** —— T2 指令"允许修改"列表只有 `globals.css` 和 `(marketing)/layout.tsx`、`(marketing)/page.tsx`。但 `/pricing` 和 `/trial` 作为 marketing 页面必须对未登录访客可访问，否则 middleware 会把它们重定向到 `/auth/login`。我仿照 T1 中 `/` 的 precedent（CodeX 接受了那次改动），在 `publicExactPaths` 中加了 `/pricing` 和 `/trial`。改动极小（+ 2 项字符串），没有触碰 session / cookie / auth 逻辑。**请 CodeX 判断是否接受。**

### 7.2 应升级给 CodeX / Human 的 blocker

**无。** 本轮没有遇到需要升级的 blocker。所有决策都在 T2 指令给定的边界内，所有超出允许文件列表的改动（仅 `middleware.ts` 一个）都有明确必要性和最小改动原则。

### 7.3 仍待项目开发者拍板的商业事实

与 T0 汇报一致，以下仍未冻结：

- Trial 天数 / 分钟数 / 是否含 Studio / 是否要求手机号 —— `TRIAL_CONFIG.frozen = false` 仍有效
- Plus / Pro 最终定价 —— 当前消费 T0 的 ¥69 / ¥179 / ¥599 / ¥299 / ¥799 / ¥2599
- 套餐中文显示名 —— 当前 API 返回英文 "Free" / "Plus" / "Pro"
- 支付方式承诺文案（支付宝 / 微信 / 对公转账）—— 页面中**没有**任何"支持支付宝/微信"的静态承诺
- 团队 / 企业套餐 —— 不在 PLAN_ORDER 白名单中，永远不会出现在 marketing 页面上

## 8. 是否已停止

**已停止。** 等待 CodeX 审核后给出下一步指令。
