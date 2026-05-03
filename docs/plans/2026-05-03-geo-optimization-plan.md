# GEO 优化方案

> Status: Proposed
> Last updated: 2026-05-03
> Scope: 面向 AI 搜索、AI 问答引用、传统搜索结果与中文商业转化的 public marketing 优化；不改动主工作流、计费真源或登录后工作台。
> Review note: 2026-05-03 V2 根据实现前代码核查补入 middleware blocker、canonical 域名真源、auth noindex、JSON-LD truth source 和 Phase 2 收敛策略。
> Review note: 2026-05-03 V3 补入 3 个落地细节：plan-level Offer frozen 判定（`Plan` 类型现状无 `frozen` 字段，用 `price_cny_fen` 代理）、auth (auth) layout 级 `noindex`（login / register / forgot-password 都是 client component，不能 page-level `export const metadata`）、`siteName` 中英文角色（"爱译视频" 是 primary，"AITrans.Video" 走 `alternateName`）。

## 1. 结论

需要做 GEO，但本项目不适合走“批量 AI 内容页 + 关键词堆砌”的路线。当前最有价值的方向是：

- 让 AI 搜索系统能稳定抓取 public marketing 页面。
- 让页面用中文清楚回答高意图问题，例如“英文长视频怎么翻译成中文配音”“一键 AI 视频翻译和长视频工作台有什么区别”“AI 视频翻译后能拿到哪些交付物”。
- 用真实产品截图、真实 demo、定价页、试用页和法律页支撑可信度。
- 继续让 Gateway 作为套餐、价格、试用、权益事实源，前端只消费事实，不重定义事实。

第一阶段应该优先补技术分发层：middleware 放行、canonical 域名对齐、`sitemap.ts`、`robots.ts`、canonical / Open Graph metadata、基础 JSON-LD。第二阶段再补 3 个高意图中文答案页，不做泛内容农场；`llms.txt` 等内容和索引数据稳定后再考虑。

## 2. GEO 的项目定义

这里的 GEO 指 Generative Engine Optimization，也就是让 ChatGPT Search、Perplexity、Google AI Overviews / AI Mode、Bing / Copilot 等 AI 搜索或生成式问答系统更容易：

- 发现页面。
- 理解品牌、产品、能力和限制。
- 在回答用户问题时引用 public 页面。
- 把高意图用户带到试用、定价、注册或联系路径。

GEO 不是一个脱离 SEO 的独立技术栈。Google 官方文档明确：出现在 AI Overviews / AI Mode 不需要特殊 AI schema 或额外机器可读文件，基础 SEO、可抓取、可索引、文本内容可见、结构化数据和可见内容一致仍是核心。

## 3. 当前项目基础

### 3.1 已具备的优势

- `frontend-next/src/app/layout.tsx` 已有中文 title、description、keywords，定位为“英文长视频变成中文配音版”。
- `frontend-next/src/app/(marketing)/page.tsx` 已形成较完整的叙事顺序：问题、真实 demo、产品证明、工作流、特性、适用场景、对比、信任、定价、FAQ、CTA。
- `frontend-next/src/components/marketing/product-proof.tsx` 使用真实 workspace 截图，能支撑“不是一次性生成工具，而是可复核、可修改、可下载的工作台”。
- `frontend-next/src/components/marketing/featured-demos*` 已有真实配音 demo 能回答“效果到底怎么样”。
- `frontend-next/src/app/(marketing)/pricing/page.tsx` 已从 Gateway 侧消费 plan facts，没有在页面里硬编码套餐数字。
- `frontend-next/src/middleware.ts` 已清楚划分 public exact paths：`/`、`/pricing`、`/trial`、`/auth`、`/terms`、`/privacy`、`/refund`、`/contact`。

### 3.2 当前缺口

截至本方案编写时，以下文件不存在：

- `frontend-next/src/app/sitemap.ts`
- `frontend-next/src/app/robots.ts`
- `frontend-next/public/llms.txt`

另外，当前 public 页面还缺少统一的 canonical URL 策略、Open Graph / Twitter metadata 策略、面向软件产品的 JSON-LD 策略，以及围绕高意图问题的独立答案页。

### 3.3 实现前必须处理的 blocker

当前 `frontend-next/src/middleware.ts` 的静态资源 early-out 正则只放行常见图片、字体、CSS、JS、视频扩展名，不包含 `xml` 和 `txt`。如果直接新增 Next.js `sitemap.ts` / `robots.ts`，未登录 crawler 请求 `/sitemap.xml` 和 `/robots.txt` 会进入鉴权分支并 302 到 `/auth/login`。

Phase 1 开工前必须先修 middleware，二选一：

- 把 `/sitemap.xml`、`/robots.txt` 加入 `publicExactPaths`。
- 或把扩展名 early-out 扩展到 `xml|txt`。

验收必须明确：未登录请求 `/sitemap.xml` 和 `/robots.txt` 返回 200，不跳转到 `/auth/login`。

## 4. 非目标

本阶段不做：

- 不做批量 AI 洗稿页。
- 不做隐藏文本、关键词堆砌、虚假评论、虚假评分或伪造案例。
- 不开放 `/admin`、`/workspace`、`/projects`、`/settings` 等登录后页面给搜索和 AI crawler。
- 不把套餐、价格、试用、权益事实复制到前端常量或内容页中。
- 不把 `llms.txt` 当成主策略。
- 不引入新的真实外部服务依赖到本地默认路径或测试路径。
- 不扩展团队席位、审核席位、完整自动续费、完整 minute-level ledgering 等后期商业化能力。
- 不改变主链路架构：TTS 仍以 `SemanticBlock` 为单位，alignment 仍 DSP-first，字幕 retiming 仍 deterministic，主目标仍是 Jianying draft / draft-first 交付。

## 5. 外部约束和依据

### 5.1 Google AI features

Google 的站长视角建议可以归纳为：

- AI Overviews / AI Mode 没有额外技术门槛，仍然复用 Google Search 的基础 SEO 要求。
- 页面需要可索引，并且允许 snippet，才有资格作为 AI features 的 supporting link。
- 重要内容要以文本形式出现在页面中。
- 结构化数据必须和页面可见内容一致。
- 不需要为了 AI features 新增特殊 schema、AI text file 或专门 markup。

对本项目的含义：

- `llms.txt` 可以做，但不能替代 sitemap、robots、metadata、可读文本和页面质量。
- 页面必须有中文可见正文，不能只依赖图片、视频或客户端渲染后的不可见状态。
- 结构化数据不能写“页面上看不到”的承诺、价格或评分。

### 5.2 robots.txt

robots.txt 用来控制 crawler 能否请求 URL，主要用于管理抓取行为，不是可靠的搜索结果移除机制。需要避免被索引的登录后页面，最好同时满足：

- 不在 sitemap 出现。
- 被 middleware / session 保护。
- 对可访问但不应索引的页面加 `noindex`。
- robots 中显式 disallow 用户数据与后台路径。

### 5.3 sitemap

sitemap 应列出希望搜索引擎展示的 canonical public URL。本项目一期 sitemap 只应包括：

- `/`
- `/pricing`
- `/trial`
- `/contact`
- `/terms`
- `/privacy`
- `/refund`

新增内容页上线后再加入 sitemap。不要把登录后工作台 URL、API URL、带 jobId 的 URL、下载 URL 放入 sitemap。

### 5.4 OpenAI / Perplexity crawler

OpenAI 将搜索展示 crawler 和训练 crawler 分开：

- `OAI-SearchBot` 用于 ChatGPT 搜索结果展示。
- `GPTBot` 用于可能参与模型训练的数据抓取。

Perplexity 也区分搜索索引 crawler 和用户触发访问：

- `PerplexityBot` 用于搜索索引。
- `Perplexity-User` 用于用户请求触发的访问。

对本项目的推荐策略是“搜索可见、训练保守”：

- 允许 `OAI-SearchBot`、`PerplexityBot`、`Googlebot`、`Bingbot` 访问 public marketing 内容。
- 对 `GPTBot`、`Google-Extended` 等训练相关 agent 先采取保守策略，除非业务明确希望内容用于训练。
- 不把用户数据、后台数据、订单数据、工作台页面暴露给任何 crawler。

## 6. 架构边界

### 6.1 Gateway 事实源边界

Gateway 仍是 plan catalog、trial rules、prices、entitlements、credits、payment provider availability 的 source of truth。GEO 页面只能消费这些事实，不能重新定义这些事实。

落地要求：

- 定价页、试用页、价格结构化数据如果需要展示数字，必须从 `getPlansSafeServer() -> /api/plans` 或等价 Gateway truth path 派生。
- 内容页可以写“支持 Free / Plus / Pro 三档”“试用无需绑卡”这类已稳定的产品事实，但具体分钟数、价格、并发、优惠、试用天数必须避免硬编码，除非该事实在 Gateway 中 frozen 并由组件读取。
- `llms.txt` 不写具体价格、试用分钟数、优惠和 provider availability。
- `Offer` schema 也必须遵守 frozen gate。注意 `Plan` 类型当前没有显式 `frozen` 字段（见 `frontend-next/src/lib/billing/types.ts`，`frozen` 只在 `TrialConfig` 上有），plan-level frozen 判定用价格代理：`plan.price_cny_fen?.monthly != null && plan.price_cny_fen.monthly > 0`；trial-level 才用 `trial.frozen === true`。两者任一不满足都不输出对应 Offer，不要输出占位价格或半静态价格。

### 6.2 public / app 边界

只允许 public marketing 页面参与索引。登录后工作区、项目结果、下载、admin、billing center 不进入 sitemap。

原因：

- 工作台页面包含用户任务、素材、下载和状态，不适合被 crawler 访问。
- 即使 middleware 会重定向未登录访问，搜索系统仍可能索引登录页或产生低质量重复 URL。
- GEO 的目标是获客和解释产品，不是暴露用户工作流状态。

auth 页面不能只依赖 robots disallow。`/auth/login`、`/auth/register`、`/auth/forgot-password` 这类页面不进 sitemap，并应在页面级 metadata 中设置 `robots: { index: false, follow: false }`，避免外链或历史抓取导致无 snippet 的低价值索引结果。

### 6.3 中文优先边界

面向中国创作者、知识付费团队、MCN、课程团队和企业内容团队。文案应自然中文化：

- CTA 用“免费试用”“查看定价”“联系顾问”“上传视频试试”等中文 SaaS 常见表达。
- 信任点突出“无需绑卡”“失败不计费”“可复核可修改”“合规授权使用”“可下载素材包”。
- 避免直译英文 SaaS 话术，例如 “supercharge your workflow” 这类表达。

### 6.4 canonical 域名边界

Phase 1 前必须先选定唯一 canonical 域名。当前代码和部署线索存在不一致：

- `gateway/notifications.py` 的 `SITE_URL` 默认值是 `https://us.aivideotrans.site`。
- `Caddyfile` 注释和站点块已经把 `aitrans.video` 当作 public host。
- 方案草案中的 sitemap 示例使用 `https://aitrans.video/sitemap.xml`。

推荐把 `aitrans.video` 作为 canonical host，但落地前需要由部署配置确认。确认后：

- `frontend-next/src/lib/seo/site.ts` 的 `siteUrl` 读取 `process.env.NEXT_PUBLIC_SITE_URL`。
- 后端 `SITE_URL` 和前端 `NEXT_PUBLIC_SITE_URL` 在 docker-compose / 部署环境中设置为同一个 canonical origin。
- `gateway/notifications.py` 的默认值也同步到 canonical host，避免邮件链接、sitemap、canonical、JSON-LD 出现双真源。
- 不在 SEO config 中再发明另一个域名 fallback；本地开发可 fallback 到 `http://localhost:3000`，生产必须显式配置。

## 7. 一期技术方案

### 7.0 先修 middleware 放行

在新增 `sitemap.ts` / `robots.ts` 前，先修改：

```text
frontend-next/src/middleware.ts
```

要求：

- 未登录访问 `/sitemap.xml` 返回 200。
- 未登录访问 `/robots.txt` 返回 200。
- 仍然不放开 `/admin`、`/workspace`、`/projects`、`/settings` 等登录后页面。
- 推荐把这两个精确路径加入 `publicExactPaths`，比泛化放行所有 `.xml` / `.txt` 更保守。

### 7.1 新增 `sitemap.ts`

新增文件：

```text
frontend-next/src/app/sitemap.ts
```

职责：

- 生成 `/sitemap.xml`。
- 只包含 canonical public URL。
- 使用统一 `siteUrl`，来自 `NEXT_PUBLIC_SITE_URL`，并与后端 `SITE_URL` 指向同一 canonical origin。
- 不把 `changeFrequency` 和 `priority` 当作优化重点；可以省略或使用保守默认值，核心是 URL 白名单和 canonical origin 正确。

首批 URL：

| URL | 原因 |
| --- | --- |
| `/` | 品牌和产品总入口 |
| `/pricing` | 商业化核心页，价格事实由 Gateway SSR |
| `/trial` | 试用转化页 |
| `/contact` | 商务和合规联系 |
| `/terms` | 法律与支付审核 |
| `/privacy` | 法律与支付审核 |
| `/refund` | 支付信任和售后规则 |

后续内容页上线后，以人工白名单方式加入，不做全路由自动扫描，避免误收登录后路由。

### 7.2 新增 `robots.ts`

新增文件：

```text
frontend-next/src/app/robots.ts
```

推荐策略：

- 默认允许 public marketing。
- 禁止 app、admin、workspace、projects、settings、tasks、notifications、usage、API、gateway proxy、job-api、下载路径。
- 在 robots 中引用 sitemap。
- 显式允许搜索型 AI crawler 抓 public 页面。
- 对训练型 crawler 保守 disallow。

建议规则草案：

```txt
User-agent: *
Allow: /
Disallow: /api/
Disallow: /job-api/
Disallow: /gateway/
Disallow: /admin/
Disallow: /workspace/
Disallow: /projects/
Disallow: /settings/
Disallow: /tasks/
Disallow: /notifications/
Disallow: /usage/
Disallow: /voices/
Disallow: /auth/

User-agent: OAI-SearchBot
Allow: /
Disallow: /api/
Disallow: /job-api/
Disallow: /gateway/
Disallow: /admin/
Disallow: /workspace/
Disallow: /projects/
Disallow: /settings/
Disallow: /tasks/
Disallow: /notifications/
Disallow: /usage/
Disallow: /voices/
Disallow: /auth/

User-agent: PerplexityBot
Allow: /
Disallow: /api/
Disallow: /job-api/
Disallow: /gateway/
Disallow: /admin/
Disallow: /workspace/
Disallow: /projects/
Disallow: /settings/
Disallow: /tasks/
Disallow: /notifications/
Disallow: /usage/
Disallow: /voices/
Disallow: /auth/

User-agent: GPTBot
Disallow: /

User-agent: Google-Extended
Disallow: /

Sitemap: {canonical-site-url}/sitemap.xml
```

实际输出时 `{canonical-site-url}` 必须替换为选定的生产 canonical origin，例如 `https://aitrans.video`，并与 `SITE_URL` / `NEXT_PUBLIC_SITE_URL` 保持一致。

注意：`/auth` 是否 disallow 需要和业务确认。如果希望“登录/注册”页面也可被搜索引擎看见，可以改为不进入 sitemap 但不 disallow，并在 auth route metadata 加 `noindex`。当前推荐是不要索引 auth 页面。

无论 robots 是否 disallow `/auth/`，auth 页面都应加页面级 `noindex`。robots 控制抓取，不是可靠的索引移除机制。

实施细节：`/auth/login`、`/auth/register`、`/auth/forgot-password` 都以 `"use client"` 开头，无法各自 `export const metadata`。统一在 `frontend-next/src/app/(auth)/layout.tsx`（layout 是 server component）export `metadata = { robots: { index: false, follow: false } }`，覆盖整个 `(auth)` route group 即可，不需要拆改三个 page 文件。

### 7.3 统一 SEO config

建议新增：

```text
frontend-next/src/lib/seo/site.ts
```

内容：

- `siteUrl`：从 `process.env.NEXT_PUBLIC_SITE_URL` 读取，与 Gateway `SITE_URL` 对齐
- `siteName`: `"爱译视频"`（中文搜索结果首选展示形式；OG `siteName` 和 `Organization.name` 都用它）
- `brandNames`: `["爱译视频", "AITrans.Video"]`，给 `Organization.alternateName` / `WebSite.alternateName` 用，让 Knowledge Graph 把中文品牌名和拉丁品牌名归一为同一实体
- `defaultTitle`
- `defaultDescription`
- `publicRoutes`
- `blockedRoutes`

收益：

- `layout.tsx`、`sitemap.ts`、`robots.ts`、JSON-LD 共用同一组站点级事实。
- 减少后续内容页写散 canonical URL 和品牌描述。

注意：

- `siteUrl` 必须来自 `NEXT_PUBLIC_SITE_URL`，并与 Gateway `SITE_URL` 对齐。
- `Organization` 的公司名、客服邮箱、付款渠道说明等合规口径不要复制到 SEO config，直接复用 `frontend-next/src/components/marketing/company-info.ts` 中的 `COMPANY_NAME`、`SUPPORT_EMAIL` 等常量。

### 7.4 canonical / Open Graph metadata

对以下页面补齐 metadata：

- `/`
- `/pricing`
- `/trial`
- `/contact`
- `/terms`
- `/privacy`
- `/refund`

每页至少包括：

- `title`
- `description`
- `alternates.canonical`
- `openGraph.title`
- `openGraph.description`
- `openGraph.url`
- `openGraph.siteName`
- `openGraph.locale = "zh_CN"`
- `openGraph.type = "website"` 或特定类型

根 layout 的范围要收窄。`frontend-next/src/app/layout.tsx` 同时包住 `(marketing)`、`(auth)` 和 `(app)` route groups，因此根层只适合放：

- `metadataBase`
- 默认 title template
- 默认 OG image
- 全站基础 theme / color-scheme 相关信息

不要在根 layout 放 `alternates.canonical: "/"`，也不要把营销首页的 OG title / description 当成所有页面兜底，否则会泄到 `/workspace/*`、`/settings/*` 等登录后页面。canonical / OG title / OG description 必须由 public 页面单独声明。

### 7.5 JSON-LD 结构化数据

建议新增小组件：

```text
frontend-next/src/components/seo/json-ld.tsx
frontend-next/src/components/seo/software-json-ld.tsx
```

一期结构化数据：

| 类型 | 页面 | 内容边界 |
| --- | --- | --- |
| `Organization` | 全站或首页 | 品牌名、URL、logo、contactPoint，可见事实 |
| `WebSite` | 首页 | 站点名称、URL、语言 |
| `SoftwareApplication` / `WebApplication` | 首页、定价页 | 产品类别、应用能力、语言、操作系统 Web，避免虚假评分 |
| `BreadcrumbList` | 内容页、定价页、试用页 | 和页面导航一致 |

谨慎项：

- `Organization`：公司名、客服邮箱、付款说明等字段直接复用 `frontend-next/src/components/marketing/company-info.ts`，不要在 SEO config 里另写一份。
- `Offer`：只有从 Gateway plan catalog 派生，且对应商业事实 `frozen === true` 时才加。不要在静态 schema 中硬编码价格；`frozen=false` 时直接不输出 `Offer` schema。
- `FAQPage`：营销 FAQ 可以保持可见文本即可。除非后续有明确搜索结果收益，不优先加 FAQ schema。
- `AggregateRating` / `Review`：没有真实可验证评价前不加。

### 7.6 `llms.txt`

`llms.txt` 降级为 Phase 2 后的可选项，不放入 Phase 1 必做范围。原因：

- 主流搜索和 AI features 没有公开承诺一定消费 `llms.txt`。
- Google 明确不要求额外 AI text file 才能进入 AI features。
- 当前更重要的是 middleware、canonical、sitemap、robots、metadata 和 JSON-LD。

如果后续要新增，可放在：

```text
frontend-next/public/llms.txt
```

定位：

- 给 AI 工具一个简短、稳定、可引用的产品摘要。
- 列出最重要 public URLs。
- 明确不应抓取用户数据、后台、任务、下载和 API。

建议内容边界：

- 写品牌、产品定位、核心能力、适用人群。
- 写 Express / Studio 的概念差异，但不写具体价格、分钟数、并发、折扣。
- 写交付物类别：中文配音视频、配音音频、字幕、翻译文本、素材包、剪映草稿能力。
- 写合规提示：用户应上传本人或已授权内容。
- 指向 `/pricing`、`/trial`、`/contact`、`/terms`、`/privacy`。

## 8. 二期内容页方案

二期不做大量内容页，先落地 3 个高意图页面。每页都必须能独立回答一个真实搜索问题，并链接回产品证明、demo、定价、试用和联系页。剩余候选页进入 backlog，由 Search Console / Bing query 数据和站内转化数据决定优先级。

### 8.1 推荐首批 URL

| URL | 优先级 | 搜索意图 | 页面目标 |
| --- | --- | --- | --- |
| `/compare/one-click-vs-workbench` | P0 | 一键 AI 视频翻译和工作台区别 | 承接对比型查询 |
| `/guide/express-vs-studio` | P0 | Express 和 Studio 怎么选 | 降低模式选择成本 |
| `/guide/video-translation-deliverables` | P0 | AI 视频翻译后能下载什么 | 强化交付物和工作台差异 |
| `/guide/jianying-draft-video-translation` | P1 | AI 视频翻译如何继续剪映精修 | 强化 Jianying draft 差异化 |
| `/guide/ai-video-translation-to-chinese-dubbing` | P1 | 英文视频怎么翻译成中文配音 | 解释完整工作流，导向试用 |
| `/use-cases/youtube-long-video-translation` | P1 | YouTube 长视频中文本地化 | 面向创作者和内容团队 |
| `/use-cases/podcast-course-localization` | P2 | 播客、课程、访谈本地化 | 面向知识内容商业化 |
| `/use-cases/b2b-training-video-localization` | P2 | 企业培训视频中文化 | 面向 B2B 采购和销售线索 |

### 8.2 单页结构模板

每个内容页建议使用同一信息结构，但不要做成机械模板：

1. H1 直接对应用户问题。
2. 首屏 80 到 120 字给出直接答案。
3. 适合谁，不适合谁。
4. 标准流程：上传或链接、转录、翻译、配音、复核、导出。
5. Express / Studio 在该场景下怎么选。
6. 能拿到哪些交付物。
7. 常见风险：版权授权、源视频音质、多人说话、字幕密度、长视频成本。
8. 产品证明：嵌入真实 demo 或截图锚点。
9. CTA：免费试用、查看定价、联系顾问。
10. 内链：到 `/pricing`、`/trial`、`/contact`、相关 guide / compare 页。

### 8.3 内容写法要求

- 首段要像答案，不像广告。
- 标题和小标题用用户会问的话。
- 每页至少有一个“限制和适用边界”段落，避免过度承诺。
- 每页都要有真实产品证据链接或截图区域。
- 不写“全网最好”“100%准确”“无限制”“永久保存”等无法支撑的表达。
- 价格、分钟数、试用数字、并发、权益只通过 Gateway-driven 组件出现。

### 8.4 中文关键词簇

关键词不是堆在页面底部，而是自然覆盖在页面问题和段落中：

- 英文视频翻译
- AI 视频翻译
- 视频翻译成中文
- 中文配音
- AI 配音
- AI 字幕
- YouTube 视频翻译
- 长视频翻译
- 课程视频翻译
- 播客翻译
- 视频本地化
- SRT 字幕导出
- 剪映草稿
- 逐句复核
- 单段重生成
- 长视频配音工作台

## 9. 内链和信息架构

### 9.1 首页承担总入口

首页继续负责：

- 品牌定位。
- 真实 demo。
- 产品截图证明。
- 工作流和交付物。
- 一键工具 vs 工作台的核心差异。
- 定价预览和 FAQ。

新增内容页后，首页可以在 FAQ 或适用场景附近加“使用指南”入口，但不要把首页改成文章列表。

### 9.2 定价页承担商业事实

定价页继续：

- 从 Gateway 获取 plan facts。
- 展示 Free / Plus / Pro。
- 展示试用 banner。
- 解释失败不计费、修改片段不必重跑全片。

内容页需要价格信息时，链接到 `/pricing`，不要复制价格表。

### 9.3 试用页承担低门槛转化

试用页需要围绕：

- 无需绑卡。
- 上传或粘贴链接试一条。
- 适合先验证英文长视频转中文配音效果。
- 任务失败不计费的信任机制。

### 9.4 法律页和 contact 支撑信任

GEO 页面应该自然链接：

- `/terms`：授权内容、服务边界、禁止内容。
- `/privacy`：数据和隐私。
- `/refund`：退款和失败不计费口径。
- `/contact`：非英文素材、大批量、企业采购、授权问题。

## 10. 监控和验收

### 10.1 技术验收

一期完成后：

- `/sitemap.xml` 可访问，只包含 public canonical URL。
- `/robots.txt` 可访问，包含 sitemap 引用。
- 未登录访问 `/sitemap.xml` 和 `/robots.txt` 返回 200，不重定向到 `/auth/login`。
- `SITE_URL` 与 `NEXT_PUBLIC_SITE_URL` 在生产环境指向同一个 canonical origin。
- public 页面 metadata 有 canonical。
- `/auth/login`、`/auth/register` 等 auth 页面有页面级 `noindex`。
- 登录后页面不在 sitemap。
- `npm run lint` 在 `frontend-next/` 通过。
- `npm run build` 在 `frontend-next/` 通过，或记录现有 unrelated blocker。
- Google Rich Results Test / Schema Markup Validator 不报核心 schema 错误。
- Search Console 可以提交 sitemap。
- Bing Webmaster Tools 可以提交 sitemap。

### 10.2 内容验收

二期内容页上线后：

- 每页 H1 对应一个明确搜索问题。
- 每页首段可独立作为 AI 答案摘要。
- 每页至少 800 到 1500 中文字，避免空薄页面。
- 每页至少有 3 个内部链接。
- 每页有产品证据或真实能力说明。
- 每页有合规和限制说明。
- 每页不硬编码价格、试用分钟数、套餐权益数字。

### 10.3 数据指标

初期看趋势，不看绝对量：

- Search Console：indexed pages、impressions、queries、CTR、average position。
- Bing Webmaster Tools：indexed URLs、crawl errors、search keywords。
- 服务端日志：`Googlebot`、`Bingbot`、`OAI-SearchBot`、`PerplexityBot` 的访问状态码和路径。
- 来源转化：来自 Google、Bing、ChatGPT、Perplexity 的 trial、register、pricing click、contact。
- 内容页内部指标：停留时间、CTA click、demo play、pricing click。

### 10.4 复盘节奏

- 上线后第 1 周：确认抓取和索引，无需判断排名。
- 第 2 到 4 周：看 impressions 和 query 分布，修页面标题、描述和内链。
- 第 6 到 8 周：决定是否扩充内容页，或把表现差的页面合并/重写。

## 11. 分期实施计划

### Phase 0: 预审、域名和 middleware blocker

目标：确认当前 public 页面和技术缺口，并先处理会导致 Phase 1 失效的 blocker。

任务：

- 记录现有 public routes。
- 确认 `sitemap.ts`、`robots.ts`、`llms.txt` 缺失。
- 记录当前首页、pricing、trial metadata。
- 确认 Gateway plan facts 消费路径。
- 决定唯一 canonical 域名，推荐候选为 `https://aitrans.video`。
- 对齐后端 `SITE_URL` 与前端 `NEXT_PUBLIC_SITE_URL`。
- 修正 `frontend-next/src/middleware.ts`，确保 `/sitemap.xml`、`/robots.txt` 未登录可 200 访问。

产出：

- 本方案文档。
- 一期实现 issue / checklist。

### Phase 1: 技术分发层

目标：让搜索和 AI crawler 能正确发现 public 页面，同时保护登录后页面。

任务：

- 新增 `frontend-next/src/lib/seo/site.ts`。
- 新增 `frontend-next/src/app/sitemap.ts`。
- 新增 `frontend-next/src/app/robots.ts`。
- 补齐首页、pricing、trial、contact、legal pages metadata。
- 在 `frontend-next/src/app/(auth)/layout.tsx` export `metadata = { robots: { index: false, follow: false } }`，覆盖整个 (auth) route group（login / register / forgot-password 都是 client component，不能各自 `export const metadata`）。
- 新增基础 JSON-LD 组件。

验收：

- `/sitemap.xml` 和 `/robots.txt` 未登录 200。
- sitemap 不含登录后页面。
- canonical 域名、邮件链接域名和 JSON-LD URL 域名一致。
- lint / build 通过或记录 unrelated blocker。

### Phase 2: 高意图内容页

目标：建立可被 AI 问答引用的中文答案层。

任务：

- 新增 guide / compare / use-cases 路由组。
- 只先写 3 页：
  - `/compare/one-click-vs-workbench`
  - `/guide/express-vs-studio`
  - `/guide/video-translation-deliverables`
- 把新增 URL 加入 sitemap。
- 首页和 footer 加少量自然内链。
- `llms.txt` 仍保持可选，等 Phase 2 内容页和索引数据稳定后再决定是否新增。

验收：

- 每页有独立 metadata、canonical、breadcrumb JSON-LD。
- 每页中文自然，不像模板页。
- 不硬编码 Gateway-owned facts。

### Phase 3: 监控和迭代

目标：确认 crawlers、索引和转化路径有效。

任务：

- 提交 sitemap 到 Search Console / Bing Webmaster Tools。
- 检查服务器日志 crawler 访问。
- 建立来源转化看板。
- 每 2 周复盘 query 和页面表现。

产出：

- GEO performance notes。
- 第二批内容页优先级。
- 是否调整 robots 中训练 crawler 策略的建议。

## 12. 风险和处理

| 风险 | 表现 | 处理 |
| --- | --- | --- |
| 前端复制 Gateway 事实 | 内容页和定价页出现不同价格或权益 | 所有商业数字通过 Gateway-driven 组件输出 |
| sitemap / robots 被 middleware 重定向 | crawler 看到 `/auth/login` 而不是 XML / TXT | 先修 middleware，未登录访问 `/sitemap.xml` 和 `/robots.txt` 必须 200 |
| canonical 域名漂移 | 邮件、canonical、sitemap、JSON-LD 指向不同域 | 生产同时配置 `SITE_URL` 与 `NEXT_PUBLIC_SITE_URL` 为同一 origin |
| 内容页空薄 | AI 搜索不引用，传统搜索也不收录 | 每页回答真实问题，加入流程、限制、证据、内链 |
| robots 误挡 public 页面 | sitemap URL 无法抓取 | 上线前用 URL inspection 和 robots tester 检查 |
| 登录页被索引 | 搜索结果出现低价值 auth 页面 | auth 页面不进 sitemap，并加页面级 noindex |
| 结构化数据夸大 | rich result 不合规或信任下降 | 只写页面可见事实，不加虚假 rating / review |
| `llms.txt` 过度承诺 | AI 工具引用错误价格或能力 | 不写动态商业数字，不写未上线能力 |
| 内容偏英文 SaaS 话术 | 中国用户不信任或不理解 | 中文优先，强调授权、试用、失败不计费、可复核 |

## 13. 建议文件清单

一期新增：

```text
frontend-next/src/lib/seo/site.ts
frontend-next/src/app/sitemap.ts
frontend-next/src/app/robots.ts
frontend-next/src/components/seo/json-ld.tsx
frontend-next/src/components/seo/software-json-ld.tsx
```

一期修改：

```text
frontend-next/src/middleware.ts
frontend-next/src/app/layout.tsx
frontend-next/src/app/(marketing)/page.tsx
frontend-next/src/app/(marketing)/pricing/page.tsx
frontend-next/src/app/(marketing)/trial/page.tsx
frontend-next/src/app/(marketing)/contact/page.tsx
frontend-next/src/app/(marketing)/terms/page.tsx
frontend-next/src/app/(marketing)/privacy/page.tsx
frontend-next/src/app/(marketing)/refund/page.tsx
frontend-next/src/app/(auth)/layout.tsx
frontend-next/src/components/marketing/site-footer.tsx
gateway/notifications.py
docker-compose.yml
```

二期新增候选：

```text
frontend-next/src/app/(marketing)/guide/ai-video-translation-to-chinese-dubbing/page.tsx
frontend-next/src/app/(marketing)/guide/express-vs-studio/page.tsx
frontend-next/src/app/(marketing)/guide/video-translation-deliverables/page.tsx
frontend-next/src/app/(marketing)/guide/jianying-draft-video-translation/page.tsx
frontend-next/src/app/(marketing)/compare/one-click-vs-workbench/page.tsx
frontend-next/src/app/(marketing)/use-cases/youtube-long-video-translation/page.tsx
frontend-next/src/app/(marketing)/use-cases/podcast-course-localization/page.tsx
frontend-next/src/app/(marketing)/use-cases/b2b-training-video-localization/page.tsx
frontend-next/public/llms.txt
```

## 14. 官方参考

- Google Search Central, AI features and your website: https://developers.google.com/search/docs/appearance/ai-features
- Google Search Central, structured data intro: https://developers.google.com/search/docs/appearance/structured-data/intro-structured-data
- Google Search Central, build and submit a sitemap: https://developers.google.com/search/docs/crawling-indexing/sitemaps/build-sitemap
- Google Search Central, robots.txt introduction: https://developers.google.com/search/docs/crawling-indexing/robots/intro
- OpenAI crawler documentation: https://platform.openai.com/docs/gptbot
- Perplexity crawler documentation: https://docs.perplexity.ai/guides/bots
- Bing search result documentation: https://support.microsoft.com/en-us/bing/how-bing-delivers-search-results
