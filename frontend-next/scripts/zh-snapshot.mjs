// uiloc:zh-snapshot — 默认 zh 字节一致回归（红线 1）+ site.ts inert 等价校验。
// frontend-next 无 JS 测试运行器，故用独立 node 脚本断言关键不变量（非引入 vitest/jest）。
// 直接 import site.ts（Node 24 原生 type-stripping）；site.ts 纯净无外部依赖、无 @/ 别名。
import { readFileSync } from "node:fs"
import { fileURLToPath, pathToFileURL } from "node:url"
import path from "node:path"
import { strict as assert } from "node:assert"

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..")

// 1) catalog 可读
const zhCommon = JSON.parse(readFileSync(path.join(root, "messages/zh/common.json"), "utf8"))
const enCommon = JSON.parse(readFileSync(path.join(root, "messages/en/common.json"), "utf8"))
assert.equal(zhCommon.appName, "爱译视频", "messages/zh/common.json appName 漂移")
assert.equal(enCommon.appName, "AITrans.Video", "messages/en/common.json appName 漂移")

// 1b) M1-hardening（2026-06-29）：skip-to-main + not-found chrome 从 layout.tsx / not-found.tsx
//     内联中文迁入 common 字典后，默认 zh 值与改造前内联字面量【逐字节】相同（红线 R1）。
assert.equal(zhCommon.skipToMain, "跳到主内容", "common.skipToMain 漂移（红线 R1）")
assert.equal(zhCommon.notFound.eyebrow, "页面提示", "common.notFound.eyebrow 漂移（红线 R1，EmptyState 默认值同源）")
assert.equal(zhCommon.notFound.title, "找不到页面", "common.notFound.title 漂移（红线 R1）")
assert.equal(
  zhCommon.notFound.description,
  "你访问的页面不存在或已移动，试试回到首页。",
  "common.notFound.description 漂移（红线 R1）",
)
assert.equal(zhCommon.notFound.actionLabel, "返回首页", "common.notFound.actionLabel 漂移（红线 R1）")

// 1c) uiloc error-boundaries（2026-06-29）：(marketing)/error.tsx、[locale]/error.tsx、(app)/error.tsx
//     三个错误边界组件的内联中文迁入 common.error 字典后，默认 zh 值与迁出前内联字面量
//     【逐字节】相同（红线 R1）。retry 为三组件共享键。
assert.equal(zhCommon.error.retry, "重试", "common.error.retry 漂移（红线 R1，三组件共享重试按钮）")
assert.equal(zhCommon.error.marketing.eyebrow, "页面异常", "common.error.marketing.eyebrow 漂移（红线 R1）")
assert.equal(zhCommon.error.marketing.title, "页面暂时无法加载", "common.error.marketing.title 漂移（红线 R1）")
assert.equal(
  zhCommon.error.marketing.description,
  "请重试一次；如果仍然失败，可以返回首页重新进入。",
  "common.error.marketing.description 漂移（红线 R1）",
)
assert.equal(zhCommon.error.marketing.actionLabel, "返回首页", "common.error.marketing.actionLabel 漂移（红线 R1）")
assert.equal(zhCommon.error.global.eyebrow, "页面异常", "common.error.global.eyebrow 漂移（红线 R1）")
assert.equal(zhCommon.error.global.title, "页面暂时无法打开", "common.error.global.title 漂移（红线 R1）")
assert.equal(
  zhCommon.error.global.description,
  "请重试一次；如果仍然失败，可以先返回工作区。",
  "common.error.global.description 漂移（红线 R1）",
)
assert.equal(zhCommon.error.global.actionLabel, "返回工作区", "common.error.global.actionLabel 漂移（红线 R1）")
assert.equal(zhCommon.error.workspace.eyebrow, "工作区异常", "common.error.workspace.eyebrow 漂移（红线 R1）")
assert.equal(zhCommon.error.workspace.title, "当前页面加载失败", "common.error.workspace.title 漂移（红线 R1）")
assert.equal(
  zhCommon.error.workspace.description,
  "请重试一次；如果仍然失败，可以先回到项目列表。",
  "common.error.workspace.description 漂移（红线 R1）",
)
assert.equal(zhCommon.error.workspace.actionLabel, "项目列表", "common.error.workspace.actionLabel 漂移（红线 R1）")

// 2) site.ts inert：默认 zh / 单参 absoluteUrl 行为与旧实现等价（红线 1），hreflang 只 zh
// site.ts 在 import 时即按 NEXT_PUBLIC_SITE_URL 求值 siteUrl；本守卫测的是 absoluteUrl/hreflang
// 的【逻辑】（相对 siteUrl 的前缀拼接），与具体 origin 无关。故先清掉环境变量，让 siteUrl
// 确定性回退到 fallback，避免 CI/Compose 注入 NEXT_PUBLIC_SITE_URL 时误红（@codex bot 指出）。
delete process.env.NEXT_PUBLIC_SITE_URL
const SITE_URL = "https://aitrans.video" // 清掉 env 后 siteUrl 的确定性 fallback
const site = await import(pathToFileURL(path.join(root, "src/lib/seo/site.ts")).href)

assert.equal(site.siteUrl, SITE_URL, "siteUrl fallback 漂移")
assert.equal(site.siteName, "爱译视频", "siteName 漂移（红线 1）")
assert.equal(site.defaultTitle, "爱译视频 · 让世界视频，开口说中文", "defaultTitle 漂移（红线 1）")

assert.equal(site.absoluteUrl("/"), SITE_URL, "absoluteUrl('/') 漂移")
assert.equal(site.absoluteUrl("/pricing"), `${SITE_URL}/pricing`, "absoluteUrl('/pricing') 漂移")
assert.equal(site.absoluteUrl("/pricing", "zh"), `${SITE_URL}/pricing`, "absoluteUrl(zh) ≠ 单参（zh 必须 inert）")
assert.equal(site.absoluteUrl("pricing"), `${SITE_URL}/pricing`, "absoluteUrl 无前导斜杠 漂移")

// UI-03g（2026-06-28）：home `/` 已加回 localizedRoutes（AnonymousTrialPanel + anonymousPreview
// 本地化完成、/en home 整页英文）→ hreflangLanguages("/") 互惠含 en（zh-Hans + en + x-default）。
// 早先 UI-03d-1-followup 因半中文 panel 临时移出（@codex #66 P2）已收回。
const hl = site.hreflangLanguages("/")
assert.deepEqual(
  hl,
  { "zh-Hans": SITE_URL, "en": `${SITE_URL}/en`, "x-default": SITE_URL },
  "hreflang('/') 翻旗后应含 zh-Hans + en + x-default（home 属 localizedRoute — UI-03g 本地化 panel 完成）"
)

// 已翻旗页（/pricing）互惠含 en：正向用例，防误把 localizedRoutes 清空导致全站无 en hreflang。
const hlPricing = site.hreflangLanguages("/pricing")
assert.deepEqual(
  hlPricing,
  { "zh-Hans": `${SITE_URL}/pricing`, "en": `${SITE_URL}/en/pricing`, "x-default": `${SITE_URL}/pricing` },
  "hreflang('/pricing') 翻旗后应含 zh-Hans + en + x-default（/pricing 属 localizedRoute）"
)

// legal 路由（/terms）未翻旗（不在 localizedRoutes）→ 只挂 zh-Hans + x-default，无 en。
// 防回归：03d-1 误把 legal 也挂 en 会在此 red（legal en 留待 UI-03c）。
const hlTerms = site.hreflangLanguages("/terms")
assert.deepEqual(
  hlTerms,
  { "zh-Hans": `${SITE_URL}/terms`, "x-default": `${SITE_URL}/terms` },
  "hreflang('/terms') 应只含 zh-Hans + x-default（legal 未翻旗，无 en — UI-03c 才加）"
)

// en 分支声明可用：前缀正确，供翻旗页消费
assert.equal(site.absoluteUrl("/pricing", "en"), `${SITE_URL}/en/pricing`, "absoluteUrl en 前缀错误")

// 3) auth 默认 zh 字节一致（UI-04 红线 1）：/auth · /auth/login · /auth/register ·
//    /auth/forgot-password 四页 + 三表单 + captcha 的串迁入 messages/zh/auth.json 后，
//    zh 值必须与改造前的内联字面量【逐字节】相同。下面钉死最易漂的标点敏感串：
//    - phone-login-form 用【半角逗号 ,】+【全角省略号 …】
//    - email-register-form 用【全角逗号 ，】+【半角三点 ...】
//    （二者历史不一致——照搬，不得"修正"。改任一值即在此处 red。）
//    注意：此守卫是【字典值级】校验（assert message catalog 值），非【渲染 DOM 级】快照——
//    不覆盖 JSX 在 rich-text <span> 周边的空白折叠漂移。frontend-next 无 JS test runner，属
//    可接受范围（单元卡允许 build+grep+node 断言）；若 zh-snapshot 后续获真 SSR/DOM 快照能力，再接入 4 页。
const zhAuth = JSON.parse(readFileSync(path.join(root, "messages/zh/auth.json"), "utf8"))

// 页壳标题/副标
assert.equal(zhAuth.register.title, "注册 AITrans.Video", "auth register.title 漂移")
assert.equal(zhAuth.register.subtitle, "默认使用手机号注册，也可以切换邮箱注册", "auth register.subtitle 漂移")
assert.equal(zhAuth.login.title, "登录 AITrans.Video", "auth login.title 漂移")
assert.equal(zhAuth.login.subtitlePassword, "使用手机号或邮箱和密码登录", "auth login.subtitlePassword 漂移")
assert.equal(zhAuth.forgot.title, "找回密码", "auth forgot.title 漂移")

// 半角逗号 + 全角省略号（phone-login-form 派系）
assert.equal(zhAuth.phoneForm.verifying, "验证中…", "auth phoneForm.verifying 必须用全角省略号 …")
assert.equal(zhAuth.phoneForm.sending, "发送中…", "auth phoneForm.sending 必须用全角省略号 …")
assert.equal(zhAuth.phoneForm.toastRegisterSuccess, "注册成功,欢迎使用", "auth phoneForm.toastRegisterSuccess 必须用半角逗号 ,")
assert.equal(zhAuth.phoneForm.toastCaptchaLoading, "人机验证仍在加载,请稍后再试", "auth phoneForm.toastCaptchaLoading 必须用半角逗号 ,")
assert.equal(zhAuth.passwordLogin.toastNetworkError, "网络错误,请重试", "auth passwordLogin.toastNetworkError 必须用半角逗号 ,")

// 全角逗号 + 半角三点（email-register-form 派系）
assert.equal(zhAuth.emailForm.verifying, "验证中...", "auth emailForm.verifying 必须用半角三点 ...")
assert.equal(zhAuth.emailForm.sending, "发送中...", "auth emailForm.sending 必须用半角三点 ...")
assert.equal(zhAuth.emailForm.toastRegisterSuccess, "邮箱注册成功，欢迎使用", "auth emailForm.toastRegisterSuccess 必须用全角逗号 ，")
assert.equal(zhAuth.emailForm.toastCaptchaLoading, "人机验证仍在加载，请稍后再试", "auth emailForm.toastCaptchaLoading 必须用全角逗号 ，")

// ICU 模板（rich-text + 占位符）：固定 chrome 字节一致，占位符 verbatim
assert.equal(zhAuth.phoneForm.codeSentTo, "已向 <highlight>{phone}</highlight> 发送验证码", "auth phoneForm.codeSentTo 模板漂移")
assert.equal(zhAuth.phoneForm.resendCountdown, "{remaining}s 后可重发", "auth phoneForm.resendCountdown 模板漂移")
assert.equal(zhAuth.phoneForm.passwordPlaceholder, "至少 {min} 位", "auth phoneForm.passwordPlaceholder 模板漂移")
assert.equal(zhAuth.forgot.codeSentTo, "验证码已发送至 <highlight>{identity}</highlight>", "auth forgot.codeSentTo 模板漂移")
assert.equal(zhAuth.emailForm.emailVerified, "邮箱已验证：<highlight>{normalizedEmail}</highlight>", "auth emailForm.emailVerified 模板漂移")
assert.equal(zhAuth.captcha.configMissing, "验证码配置缺失（{var} 未设置）", "auth captcha.configMissing 模板漂移")
assert.equal(zhAuth.passwordLogin.errorCsrfOriginRejected, "请求来源校验失败，请确认正在使用 {url} 访问，刷新页面后重试。", "auth passwordLogin.errorCsrfOriginRejected 模板漂移")

// 4) marketing 默认 zh 字节一致（UI-03a 红线 1）：FAQ / 对比表 / 导航 / 页脚 chrome
//    迁入 messages/zh/marketing.json 后，zh 值必须与改造前内联字面量【逐字节】相同。
//    钉死标点/全半角/破折号敏感的代表串（任一漂移即在此 red）。改任一值都视为 zh 渲染回归。
const zhMkt = JSON.parse(readFileSync(path.join(root, "messages/zh/marketing.json"), "utf8"))

// nav / footer chrome（短串，标点敏感）
assert.equal(zhMkt.nav.enterWorkspace, "进入工作台", "marketing.nav.enterWorkspace 漂移")
assert.equal(zhMkt.nav.trialCta, "免费开始试用", "marketing.nav.trialCta 漂移")
assert.equal(zhMkt.nav.ariaBrandHome, "AITrans.Video 首页", "marketing.nav.ariaBrandHome 漂移")
assert.equal(
  zhMkt.footer.tagline,
  "爱译视频，让世界视频开口说中文。专注长视频的 AI 翻译配音工作台，支持中文字幕、中文配音、多格式导出和逐句修改。",
  "marketing.footer.tagline 漂移",
)
// 版权行 ICU 模板（年份占位符 verbatim、间隔号 · 中点 verbatim）
assert.equal(
  zhMkt.footer.copyright,
  "© {year} 爱译视频 AITrans.Video · 长视频翻译配音工作台",
  "marketing.footer.copyright 模板漂移",
)

// comparison：破折号 1–3（en dash U+2013）+ 全角斜杠分隔，最易被"修正"成连字符
assert.equal(
  zhMkt.comparison.rows[0].workbench,
  "支持 1–3 小时长视频，针对访谈、课程、播客优化",
  "marketing.comparison.rows[0].workbench 破折号/标点漂移",
)
assert.equal(zhMkt.comparison.headerWorkbench, "爱译视频工作台", "marketing.comparison.headerWorkbench 漂移")
assert.equal(zhMkt.comparison.labelOneClick, "一键生成工具：", "marketing.comparison.labelOneClick 全角冒号漂移")

// faq：marquee 提示用间隔号 ·；supportNudge 用全角直角引号「」+ 全角问号？
assert.equal(
  zhMkt.faq.marqueeHint,
  "鼠标悬停可暂停自动滚动 · 完整问答见",
  "marketing.faq.marqueeHint 间隔号漂移",
)
assert.equal(
  zhMkt.faq.supportNudge,
  "还有疑问？点右下角「客服」浮窗，先 AI 后人工。",
  "marketing.faq.supportNudge 标点漂移",
)
// faq 组数与顺序（pricing = general + pricingExtra；不变量 5 的源完整性）
assert.equal(zhMkt.faq.general.length, 8, "marketing.faq.general 应为 8 条（home 集）")
assert.equal(zhMkt.faq.pricingExtra.length, 3, "marketing.faq.pricingExtra 应为 3 条（pricing 追加）")
assert.equal(
  zhMkt.faq.general[0].q,
  "为什么你们强调长视频？",
  "marketing.faq.general[0].q 漂移（全角问号）",
)

// 4b) UI-03b 内联重文案（hero / pricing / trial）默认 zh 字节一致（红线 R1）：
//     这些是转化关键文案，标点/全半角/破折号/空格最易被"修正"。任一漂移即 red =
//     默认 zh 渲染回归。下面钉死代表性串：
//   - hero.title 用 next-intl rich-text <br></br> 标签（不得退回字面 <br> 或换行）
//   - hero.lead 用全角破折号 ——（U+2014 ×2，不得改连字符）+ 间隔号无关
//   - hero.trustLine / playerHint 用间隔号 ·（U+00B7）与全角斜杠分隔
//   - pricingGrid 单位用全角斜杠+空格"/ 月"；额度模板用全角括号（约 …）
//   - trialBanner / trial 的 {studio} 占位紧贴"额度"与"。"之间（无多余空格），
//     与改造前 `${... ? "与 Studio 精校模式" : ""}。` 拼接逐字节一致
assert.equal(zhMkt.hero.eyebrow, "爱译视频 · AITrans.Video", "marketing.hero.eyebrow 间隔号漂移")
assert.equal(zhMkt.hero.title, "让世界视频，<br></br>开口说中文", "marketing.hero.title rich-text <br> 标签漂移")
assert.equal(
  zhMkt.hero.lead,
  "把英文长视频变成可发布的中文配音版。免注册先预览效果——前 3 分钟中文配音，满意再注册下载、生成完整视频。",
  "marketing.hero.lead 全角破折号/标点漂移",
)
assert.equal(
  zhMkt.hero.trustLine,
  "免注册试用 · 英文转中文 · 失败不计费 · 支持长视频",
  "marketing.hero.trustLine 间隔号漂移",
)
assert.equal(
  zhMkt.hero.playerHint,
  "鼠标移到画面上自动播放，点左上角 <strong>开启声音</strong> 试听；右上角切换 <strong>英文原片 / 中文配音</strong> 对比。",
  "marketing.hero.playerHint rich-text/空白漂移",
)
assert.equal(zhMkt.pricingGrid.unitMonthly, "/ 月", "marketing.pricingGrid.unitMonthly 全角斜杠/空格漂移")
assert.equal(
  zhMkt.pricingGrid.benefitGrantWithMinutes,
  "每月 {credits} 点处理额度（约 {expMin} 分钟 Express / {studioMin} 分钟 Studio 标准）",
  "marketing.pricingGrid.benefitGrantWithMinutes 全角括号/占位符漂移",
)
assert.equal(zhMkt.pricingGrid.benefitStudio, "Studio 精校模式（支持人工复核）", "marketing.pricingGrid.benefitStudio 全角括号漂移")
assert.equal(
  zhMkt.trialBanner.descriptionWithNumbers,
  "注册即享 {days} 天试用，含 {minutes} 分钟源视频额度{studio}。试用结束不会自动扣费，你的账户信息和已购点数会一直保留。",
  "marketing.trialBanner.descriptionWithNumbers {studio} 占位/标点漂移",
)
assert.equal(zhMkt.trialBanner.studioSuffix, "与 Studio 精校模式", "marketing.trialBanner.studioSuffix 漂移")
assert.equal(
  zhMkt.trial.leadWithNumbers,
  "注册即享 {days} 天试用，含 {minutes} 分钟源视频额度{studio}。亲自验证对齐质量与配音自然度。试用结束后不会自动扣费，账户信息和已购点数也会保留下来。",
  "marketing.trial.leadWithNumbers {studio} 占位/标点漂移",
)
assert.equal(zhMkt.trial.asideNote1, "· 无需绑定支付方式", "marketing.trial.asideNote1 间隔号漂移")
// pricingAssurance：lead 的两段中文之间是【单空格】（JSX 折叠换行+缩进的等价渲染），
// 不得回退成换行/多空格；paymentChannelNote 是 company-info 移交到 03b 消费点的字典化值
assert.equal(
  zhMkt.pricingAssurance.lead,
  "AITrans.Video 为在线数字化服务。用户完成支付后，可在账户内创建翻译任务、查看历史项目、下载交付结果并继续人工复核。 不同套餐解锁的是处理时长、并发能力、工作台模式和下载权限。",
  "marketing.pricingAssurance.lead 折叠空格/标点漂移",
)
assert.equal(
  zhMkt.pricingAssurance.paymentChannelNote,
  "付款方式以站内结算页展示为准，支付成功后，相应套餐权益或处理额度会自动发放至当前账户。",
  "marketing.pricingAssurance.paymentChannelNote 漂移（company-info 移交值）",
)

// 4c) UI-03b 收尾：6 个 rendering-CJK leak 组件（primaryCta / planCardCta /
//     anonymousTrialLauncher / heroSamplePlayer / sealStamp / trialDetails）的内联中文
//     迁入字典后，默认 zh 值必须与改造前组件源【逐字节】相同（红线 R1）。下面钉死每个
//     子 namespace 的代表性【非 ICU】串（含半/全角标点、间隔号 · U+00B7 敏感处）；任一漂移
//     即 red = 默认 zh 渲染回归。ICU 占位串（allowanceDays/upgrade 等）走 key-parity 即可。
assert.equal(zhMkt.primaryCta.guest, "免费开始试用", "marketing.primaryCta.guest 漂移")
assert.equal(zhMkt.planCardCta.contact, "联系我们", "marketing.planCardCta.contact 漂移")
assert.equal(zhMkt.anonymousTrialLauncher.cta, "立即试用", "marketing.anonymousTrialLauncher.cta 漂移")
assert.equal(zhMkt.heroSamplePlayer.tabDubbed, "中文配音", "marketing.heroSamplePlayer.tabDubbed 漂移")
assert.equal(zhMkt.heroSamplePlayer.tabOriginal, "英文原片", "marketing.heroSamplePlayer.tabOriginal 漂移")
assert.equal(zhMkt.sealStamp.ariaLabel, "AITrans.Video 章印", "marketing.sealStamp.ariaLabel 漂移")
assert.equal(zhMkt.trialDetails.benefit1Title, "体验完整工作流", "marketing.trialDetails.benefit1Title 漂移")
assert.equal(zhMkt.trialDetails.allowanceTitle, "试用权益", "marketing.trialDetails.allowanceTitle 漂移")

// 4d) UI-03e：4 个上半区营销组件（painPoints / productProof / workflowShowcase /
//     featuredDemos）的内联中文迁入字典后，默认 zh 值必须与改造前组件源【逐字节】相同
//     （红线 R1）。下面钉死每个子 namespace 的代表性串，重点覆盖标点/全半角/破折号/ICU 敏感处：
//     - closing 钉死全角弯引号 “一键生成” + <strong> rich 标签
//     - realUi 钉死 ICU 占位符 {n}
//     - workflowShowcase.steps[3].body / featuredDemos.lead 钉死全角破折号 ——（U+2014）
//     任一漂移即 red = 默认 zh 渲染回归。ICU/纯结构串走 key-parity 即可。
assert.equal(zhMkt.painPoints.eyebrow, "为什么需要专门的工作台", "marketing.painPoints.eyebrow 漂移")
assert.equal(zhMkt.painPoints.points[0].title, "视频太长", "marketing.painPoints.points[0].title 漂移")
assert.equal(
  zhMkt.painPoints.closing,
  "爱译视频不是只做“一键生成”，而是把<strong>长视频翻译、AI 配音、多格式交付和逐句修改</strong>放进同一个工作台。",
  "marketing.painPoints.closing 全角弯引号/<strong> rich 标签漂移",
)
assert.equal(zhMkt.productProof.eyebrow, "真实产品证明", "marketing.productProof.eyebrow 漂移")
assert.equal(zhMkt.productProof.realUi, "真实界面 {n}", "marketing.productProof.realUi ICU 占位符漂移")
assert.equal(
  zhMkt.workflowShowcase.heading,
  "从英文视频到中文成片，四步完成",
  "marketing.workflowShowcase.heading 漂移",
)
assert.equal(
  zhMkt.workflowShowcase.steps[3].body,
  "下载中文配音视频、音频、字幕、素材包，或直接导出剪映草稿——在剪映里继续精剪不必从零铺时间线。",
  "marketing.workflowShowcase.steps[3].body 全角破折号漂移",
)
assert.equal(
  zhMkt.featuredDemos.lead,
  "直接听一段——译文是否像人话、配音是否自然、节奏是否对得上原片。",
  "marketing.featuredDemos.lead 全角破折号漂移",
)
assert.equal(zhMkt.featuredDemos.tabDubbed, "中文配音版", "marketing.featuredDemos.tabDubbed 漂移")

// 4e) UI-03f：5 个下半区营销组件（features / suitedScenarios / trustBanner /
//     pricingPreview / finalCta）的内联中文迁入字典后，默认 zh 值必须与改造前组件源
//     【逐字节】相同（红线 R1）。下面钉死每个子 namespace 的代表性串，重点覆盖标点/全半角
//     /破折号/间隔号/rich 敏感处：
//     - features.heading 钉死全角弯引号 “翻译” / “能发布”（U+201C/U+201D）
//     - features.items[0].body 钉死 en-dash 1–3（U+2013）—— 全等断言
//     - suitedScenarios.scenarios[1].tag 钉死【半角空格 + 半角斜杠 + 半角空格】
//     - finalCta.heading 钉死 rich <br></br> 标签 + 全角逗号在 <br> 之前
//     - finalCta.trustLine 钉死间隔号 ·（U+00B7）分隔
//     任一漂移即 red = 默认 zh 渲染回归。ICU/纯结构串走 key-parity 即可。
assert.equal(
  zhMkt.features.heading,
  "把“翻译”做完，把“能发布”做对",
  "marketing.features.heading 全角弯引号漂移",
)
assert.equal(
  zhMkt.features.items[0].body,
  "最长支持 180 分钟单条视频，适合 1–3 小时的访谈、课程、播客、演讲、纪录片解读，不只适合几十秒短视频试水。",
  "marketing.features.items[0].body en-dash 1–3 漂移",
)
assert.equal(
  zhMkt.suitedScenarios.scenarios[1].tag,
  "视频号 / 抖音创作者",
  "marketing.suitedScenarios.scenarios[1].tag 空格+斜杠漂移",
)
assert.equal(zhMkt.trustBanner.promises[3].title, "项目保留 7 天", "marketing.trustBanner.promises[3].title 漂移")
assert.equal(zhMkt.pricingPreview.fullComparisonCta, "查看完整套餐对比", "marketing.pricingPreview.fullComparisonCta 漂移")
assert.equal(
  zhMkt.finalCta.heading,
  "把下一支海外英文视频，<br></br>变成中文配音版",
  "marketing.finalCta.heading rich <br> 标签/逗号位置漂移",
)
assert.equal(
  zhMkt.finalCta.trustLine,
  "英文转中文 · 无需绑卡 · 7 天试用 · 失败不计费 · 支持长视频",
  "marketing.finalCta.trustLine 间隔号漂移",
)

// 4f) UI-03g：AnonymousTrialPanel + anonymousPreview.ts 内联中文迁入 marketing.anonymousTrial
//     字典后，默认 zh 值必须与改造前组件源【逐字节】相同（红线 R1）。下面钉死标点/全半角/破折号/
//     间隔号/直角引号最敏感的代表串（任一漂移即 red = 默认 zh 渲染回归）：
//     - uploadStage.* / stage.* 用全角省略号 …（U+2026，不得退回半角三点 ...）
//     - footer / uploadZone.hint 用间隔号 ·（U+00B7）分隔
//     - failed.keepNote 用全角直角引号「」+ 全角句号。
//     - processing.hint 用 en-dash 2–5（U+2013，不得改连字符）+ 全角逗号
//     - consent.cloneOptInTitle 用全角括号（）（U+FF08/U+FF09）
//     - previewDuration ICU：minutes 分支【{value} 分钟】、other 分支【{value} 秒】（值与空格 verbatim）
const zhAt = zhMkt.anonymousTrial
assert.equal(zhAt.uploadStage.hashing, "校验文件…", "anonymousTrial.uploadStage.hashing 必须用全角省略号 …")
assert.equal(zhAt.uploadStage.merging, "合并校验中…", "anonymousTrial.uploadStage.merging 必须用全角省略号 …")
assert.equal(zhAt.uploadStage.uploading, "上传中…", "anonymousTrial.uploadStage.uploading 必须用全角省略号 …")
assert.equal(zhAt.stage.queued, "等待处理…", "anonymousTrial.stage.queued 全角省略号漂移")
assert.equal(zhAt.stage.fallback, "处理中…", "anonymousTrial.stage.fallback 全角省略号漂移")
assert.equal(
  zhAt.footer,
  "本地视频 · 前 {duration}预览 · 带水印{expressSuffix}",
  "anonymousTrial.footer 间隔号/占位符漂移",
)
assert.equal(zhAt.footerExpressSuffix, " · 快捷版真实管线", "anonymousTrial.footerExpressSuffix 间隔号/前导空格漂移")
assert.equal(
  zhAt.uploadZone.hint,
  "MP4 · MOV · M4V · WebM · 最大 {maxUploadMb}MB",
  "anonymousTrial.uploadZone.hint 间隔号/占位符漂移",
)
assert.equal(
  zhAt.failed.keepNote,
  "已上传的视频仍然保留，点击「重试」无需重新上传。",
  "anonymousTrial.failed.keepNote 全角直角引号「」/句号漂移",
)
assert.equal(
  zhAt.processing.hint,
  "配音预览生成中，通常需要 2–5 分钟，请稍候",
  "anonymousTrial.processing.hint en-dash 2–5 / 全角逗号漂移",
)
assert.equal(zhAt.consent.cloneOptInTitle, "（可选）克隆我的原声音色", "anonymousTrial.consent.cloneOptInTitle 全角括号漂移")
assert.equal(
  zhAt.previewDuration,
  "{unit, select, minutes {{value} 分钟} other {{value} 秒}}",
  "anonymousTrial.previewDuration ICU select/分钟·秒 文案漂移",
)
assert.equal(
  zhAt.ready.watermarkNote,
  "（带水印，前 {duration}）",
  "anonymousTrial.ready.watermarkNote 全角括号/逗号/占位符漂移",
)

// 4g) chunkedUpload i18n（2026-06-28，UI-03g 5-lens LOW #2 收尾）：分片上传错误 token 的 zh 字典值
//     必须与 src/lib/upload/chunkedUpload.ts 里原中文 throw 串【逐字节】相同（红线 R1）。chunkedUpload
//     抛 ChunkedUploadError(code, 原中文, params)，面板读 .code → t("uploadError." + code, params)；
//     登录态工作台仍读 .message → 原中文（双消费方各取所需）。下面钉死全角括号（）/全角逗号，/ICU
//     占位符（{status}/{partIndex}/{maxMb}/{detail}，纯文本替换不加千分位）最敏感的串：
const zhUe = zhAt.uploadError
assert.equal(zhUe.hash_failed, "文件哈希计算失败", "anonymousTrial.uploadError.hash_failed 漂移")
assert.equal(zhUe.hash_worker_failed, "哈希 Worker 启动失败", "anonymousTrial.uploadError.hash_worker_failed 漂移")
assert.equal(zhUe.chunk_init_failed, "上传初始化失败（{status}）", "anonymousTrial.uploadError.chunk_init_failed 全角括号/占位符漂移")
assert.equal(zhUe.part_upload_failed, "分片 {partIndex} 上传失败（{status}）", "anonymousTrial.uploadError.part_upload_failed 全角括号/占位符/空格漂移")
assert.equal(zhUe.part_network_retried, "分片 {partIndex} 网络错误，已重试", "anonymousTrial.uploadError.part_network_retried 全角逗号/占位符漂移")
assert.equal(zhUe.part_upload_failed_final, "分片 {partIndex} 上传失败", "anonymousTrial.uploadError.part_upload_failed_final 占位符/空格漂移")
assert.equal(zhUe.merge_verify_failed, "合并校验失败（{detail}）", "anonymousTrial.uploadError.merge_verify_failed 全角括号/占位符漂移")
assert.equal(zhUe.merge_status_failed, "查询合并状态失败", "anonymousTrial.uploadError.merge_status_failed 漂移")
assert.equal(zhUe.merge_verify_timeout, "合并校验超时，请稍后在任务页重试", "anonymousTrial.uploadError.merge_verify_timeout 全角逗号漂移")
assert.equal(zhUe.merge_verify_timeout_anon, "合并校验超时，请稍后重试", "anonymousTrial.uploadError.merge_verify_timeout_anon 全角逗号漂移")
assert.equal(zhUe.file_too_large_chunked, "文件超过 {maxMb}MB 上限，请压缩后重试", "anonymousTrial.uploadError.file_too_large_chunked 占位符/全角逗号/空格漂移")

// 5) seo 默认 zh 字节一致：site 级标题/描述与 site.ts 顶层常量同源同值（红线 1）
const zhSeo = JSON.parse(readFileSync(path.join(root, "messages/zh/seo.json"), "utf8"))
assert.equal(zhSeo.site.name, "爱译视频", "seo.site.name 漂移")
assert.equal(zhSeo.site.defaultTitle, site.defaultTitle, "seo.site.defaultTitle 必须与 site.ts defaultTitle 同值")
assert.equal(
  zhSeo.site.defaultDescription,
  site.defaultDescription,
  "seo.site.defaultDescription 必须与 site.ts defaultDescription 同值",
)
// site.ts localeSeo.zh 仍镜像顶层常量（INERT zh 路径不变）
assert.equal(site.localeSeo.zh.siteName, site.siteName, "localeSeo.zh.siteName ≠ 顶层 siteName（zh 必须 inert）")
assert.equal(site.localeSeo.zh.defaultTitle, site.defaultTitle, "localeSeo.zh.defaultTitle ≠ 顶层（zh 必须 inert）")
assert.equal(
  site.localeSeo.zh.defaultDescription,
  site.defaultDescription,
  "localeSeo.zh.defaultDescription ≠ 顶层（zh 必须 inert）",
)

// 5b) UI-03d-1（翻旗）+ P2-2（去事实化，@codex #66 采纳方案 2）：pricing/trial generateMetadata +
//     breadcrumb 读 seo 字典。title/ogTitle/breadcrumb 仍与改造前内联字面量【逐字节】相同
//     （红线 R1，钉死全角间隔号 ·）。**description 已去事实化**——移除 Free/Plus/Pro/180 分钟/无需
//     绑卡等 gateway 真源事实（避免 SEO 摘要/OG 与 gateway 漂移）；此处钉死的是**新去事实化文案**，
//     断言里写明「不得含 gateway 事实」以防回退。
assert.equal(zhSeo.pricing.title, "定价", "seo.pricing.title 漂移（红线 R1）")
assert.equal(zhSeo.pricing.ogTitle, "定价 · 爱译视频", "seo.pricing.ogTitle 间隔号/漂移（红线 R1）")
assert.equal(
  zhSeo.pricing.description,
  "面向长视频创作者的 AI 翻译配音定价。按需要的处理能力与工作台模式选择套餐，从导入、翻译、配音到逐句复核与多格式导出一站完成。",
  "seo.pricing.description 漂移（P2-2 去事实化文案，不得含 Free/Plus/Pro/分钟数/计费政策等 gateway 事实）",
)
assert.equal(zhSeo.trial.title, "免费试用", "seo.trial.title 漂移（红线 R1）")
assert.equal(zhSeo.trial.ogTitle, "免费试用 · 爱译视频", "seo.trial.ogTitle 间隔号/漂移（红线 R1）")
assert.equal(
  zhSeo.trial.description,
  "免费体验 AITrans.Video 的完整视频翻译配音工作流：从导入、翻译、配音到逐句复核与导出，注册即可开始。",
  "seo.trial.description 漂移（P2-2 去事实化文案，不得含无需绑卡/试用扣费政策等 gateway 事实）",
)
assert.equal(zhSeo.breadcrumb.home, "首页", "seo.breadcrumb.home 漂移（红线 R1）")
assert.equal(zhSeo.breadcrumb.pricing, "定价", "seo.breadcrumb.pricing 漂移（红线 R1）")
assert.equal(zhSeo.breadcrumb.trial, "免费试用", "seo.breadcrumb.trial 漂移（红线 R1）")

// 6) en seo 双源同步守卫（UI-03a 多 lens：messages/en/seo.json 与 site.ts localeSeo.en
//    各存一份 en SEO 标题/描述，当前靠手工保持一致。把『同源同值』从注释承诺升级为机器守卫，
//    防止 03d 接入 generateMetadata 前两份悄悄漂移（03d 须收敛为单一真源消费）。
const enSeo = JSON.parse(readFileSync(path.join(root, "messages/en/seo.json"), "utf8"))
assert.equal(enSeo.site.name, site.localeSeo.en.siteName, "en seo.site.name ≠ site.ts localeSeo.en.siteName（en 双源漂移）")
assert.equal(enSeo.site.defaultTitle, site.localeSeo.en.defaultTitle, "en seo.site.defaultTitle ≠ localeSeo.en.defaultTitle（en 双源漂移）")
assert.equal(
  enSeo.site.defaultDescription,
  site.localeSeo.en.defaultDescription,
  "en seo.site.defaultDescription ≠ localeSeo.en.defaultDescription（en 双源漂移）",
)

// 7) UI-05（App 中央字典）：status/stage/error/expiry/review/progress chrome 从
//    types/jobs.ts、features/jobs/{presentation,stageMetadata,expiry}.ts、status-badge.tsx
//    的内联中文迁入 messages/{zh,en}/app.json 后，默认 zh 值必须与改造前内联字面量
//    【逐字节】相同（红线 R1）。下面钉死每个子 namespace 标点/全半角/间隔号/占位符最敏感的
//    代表串（任一漂移即 red = 默认 zh 渲染回归）：
//    - status.resynthesizing 用间隔号 ·（U+00B7）+ ICU {n}（不得退回字面 ${} 或半角点）
//    - stage.none 与改造前 getStageLabel(null) fallback「待开始」同值
//    - error.*.suggestion 含全角句号。/全角逗号，；ingestion 含半角阿拉伯数字 10
//    - secondary.youtubePrefix/projectPrefix 用间隔号 · + ICU {id}（前缀 chrome，id 是 content）
//    - expiry.daysLeft/hoursLeft 用 ICU {days}/{hours} + 前导/尾随空格 verbatim（zh 非复数）
//    - review.descriptionForStage/actionForStage 用 ICU {stageLabel}，与原模板拼接逐字节一致
//    - progress.* 是 sanitizedProgressMessages 的 4 个 zh 显示值（key 仍为后端 EN 串）
const zhApp = JSON.parse(readFileSync(path.join(root, "messages/zh/app.json"), "utf8"))

// status：11 状态 + resynthesizing ICU（间隔号 + 占位符）
assert.equal(zhApp.status.queued, "待开始", "app.status.queued 漂移（红线 R1）")
assert.equal(zhApp.status.running, "处理中", "app.status.running 漂移（红线 R1）")
assert.equal(zhApp.status.waiting_for_review, "等待审核", "app.status.waiting_for_review 漂移（红线 R1）")
assert.equal(zhApp.status.purged, "已清理", "app.status.purged 漂移（红线 R1）")
assert.equal(zhApp.status.archiving, "归档中", "app.status.archiving 漂移（红线 R1）")
assert.equal(
  zhApp.status.resynthesizing,
  "重合成中 · 第 {n} 次修改",
  "app.status.resynthesizing 间隔号 ·/ICU {n} 漂移（红线 R1）",
)

// stage：9 阶段 + none fallback（与 getStageLabel(null) 旧值「待开始」同值）
assert.equal(zhApp.stage.none, "待开始", "app.stage.none 漂移（getStageLabel(null) fallback，红线 R1）")
assert.equal(zhApp.stage.draft, "草稿与配音", "app.stage.draft 漂移（红线 R1）")
assert.equal(zhApp.stage.legacy_process_output, "输出完成", "app.stage.legacy_process_output 漂移（红线 R1）")
assert.equal(zhApp.stage.voice_selection_review, "音色选择", "app.stage.voice_selection_review 漂移（红线 R1）")

// stageDescription：9 条阶段描述（全角句号。结尾）
assert.equal(
  zhApp.stageDescription.media_understanding,
  "提取媒体信息并建立后续处理上下文。",
  "app.stageDescription.media_understanding 漂移（红线 R1）",
)
assert.equal(zhApp.stageDescription.draft, "生成配音和对齐。", "app.stageDescription.draft 漂移（红线 R1）")

// reviewStageDescription：5 条（全角逗号，+ 句号。）
assert.equal(
  zhApp.reviewStageDescription.speaker_review,
  "请先确认说话人名称和片段归属，然后继续下一步。",
  "app.reviewStageDescription.speaker_review 漂移（红线 R1）",
)
assert.equal(
  zhApp.reviewStageDescription.voice_selection_review,
  "请为每位说话人选择或克隆配音音色，然后继续下一步。",
  "app.reviewStageDescription.voice_selection_review 漂移（红线 R1）",
)

// review：helper 内联 fallback（ICU {stageLabel} 拼接与原模板逐字节一致）
assert.equal(zhApp.review.pendingTitle, "等待审核", "app.review.pendingTitle 漂移（红线 R1）")
assert.equal(zhApp.review.genericStage, "审核", "app.review.genericStage 漂移（红线 R1）")
assert.equal(
  zhApp.review.descriptionForStage,
  "当前任务正在等待{stageLabel}，请先完成处理。",
  "app.review.descriptionForStage ICU/标点漂移（红线 R1）",
)
assert.equal(zhApp.review.descriptionGeneric, "当前任务正在等待审核处理。", "app.review.descriptionGeneric 漂移（红线 R1）")
assert.equal(zhApp.review.actionForStage, "处理{stageLabel}", "app.review.actionForStage ICU 漂移（红线 R1）")
assert.equal(zhApp.review.actionGeneric, "继续处理审核", "app.review.actionGeneric 漂移（红线 R1）")
assert.equal(
  zhApp.review.needsReviewFirst,
  "当前任务需要先完成审核，然后才能继续。",
  "app.review.needsReviewFirst 漂移（红线 R1）",
)

// error：5 分类 {label,suggestion} + noDetail fallback（半角数字 10 + 全角标点）
assert.equal(zhApp.error.noDetail, "当前没有更多失败说明。", "app.error.noDetail 漂移（红线 R1）")
assert.equal(zhApp.error.ingestion.label, "音频上传或转录失败", "app.error.ingestion.label 漂移（红线 R1）")
assert.equal(
  zhApp.error.ingestion.suggestion,
  "长视频容易上传失败，建议使用 10 分钟以内的视频重试。",
  "app.error.ingestion.suggestion 半角数字 10/全角标点漂移（红线 R1）",
)
assert.equal(zhApp.error.voiceclone.label, "音色克隆失败", "app.error.voiceclone.label 漂移（红线 R1）")
assert.equal(zhApp.error.translation.label, "翻译生成失败", "app.error.translation.label 漂移（红线 R1）")
assert.equal(zhApp.error.alignment.label, "时长对齐失败", "app.error.alignment.label 漂移（红线 R1）")
assert.equal(zhApp.error.generic.label, "处理失败", "app.error.generic.label 漂移（红线 R1）")
assert.equal(zhApp.error.generic.suggestion, "请查看项目详情了解更多信息。", "app.error.generic.suggestion 漂移（红线 R1）")

// title / secondary：未命名视频 fallback（chrome）+ 前缀 chrome（id 是 content，走 ICU 占位符）
assert.equal(zhApp.title.untitled, "未命名视频", "app.title.untitled 漂移（红线 R1，getJobDisplayTitle fallback）")
assert.equal(
  zhApp.secondary.youtubePrefix,
  "YouTube 视频 · {id}",
  "app.secondary.youtubePrefix 间隔号 ·/ICU {id} 漂移（红线 R1，id 是 content 透传）",
)
assert.equal(
  zhApp.secondary.projectPrefix,
  "项目记录 · {id}",
  "app.secondary.projectPrefix 间隔号 ·/ICU {id} 漂移（红线 R1）",
)

// expiry：4 串（ICU {days}/{hours} + 前导/尾随空格 verbatim；zh 非复数）
assert.equal(zhApp.expiry.deletingSoon, "即将删除", "app.expiry.deletingSoon 漂移（红线 R1）")
assert.equal(zhApp.expiry.daysLeft, "{days} 天后过期", "app.expiry.daysLeft ICU {days}/空格漂移（红线 R1）")
assert.equal(zhApp.expiry.hoursLeft, "{hours} 小时后过期", "app.expiry.hoursLeft ICU {hours}/空格漂移（红线 R1）")
assert.equal(zhApp.expiry.underOneHour, "不到 1 小时后过期", "app.expiry.underOneHour 半角数字 1/空格漂移（红线 R1）")

// progress：sanitizedProgressMessages 的 4 个 zh 显示值（lookup key 仍为后端 EN 串，见 presentation.ts）
assert.equal(zhApp.progress.completed, "任务已完成。", "app.progress.completed 漂移（红线 R1）")
assert.equal(zhApp.progress.queued, "任务已进入队列。", "app.progress.queued 漂移（红线 R1）")
assert.equal(
  zhApp.progress.reviewingSpeakers,
  "正在处理说话人审核结果。",
  "app.progress.reviewingSpeakers 漂移（红线 R1）",
)
assert.equal(zhApp.progress.starting, "任务已开始处理。", "app.progress.starting 漂移（红线 R1）")

// 8) UI-09（errors namespace）：客户端错误显示层本地化（lib/api/error-localization.ts）。
//    errors.status.* / errors.timeout 的 zh 值＝client.ts statusFallbackMessage / timeout 串
//    【逐字节】照搬；errors.generic zh＝lib/api/errors.ts getErrorMessage 兜底串照搬（红线 R1）。
//    这些都是【前端自有】串（非后端文案），故无后端漂移风险；改任一值即 = 默认 zh 失败路径回归。
//    timeout 的 {seconds} 占位符 verbatim（client.ts 写入 payload.timeoutSeconds，显示层 ICU 填充）。
const zhErrors = JSON.parse(readFileSync(path.join(root, "messages/zh/errors.json"), "utf8"))
assert.equal(zhErrors.status.unauthorized, "登录已过期，请重新登录", "errors.status.unauthorized 漂移（红线 R1，client.ts statusFallbackMessage 401）")
assert.equal(zhErrors.status.forbidden, "没有权限执行此操作", "errors.status.forbidden 漂移（红线 R1，403）")
assert.equal(zhErrors.status.notFound, "请求的资源不存在", "errors.status.notFound 漂移（红线 R1，404）")
assert.equal(zhErrors.status.serviceUnavailable, "服务暂时不可用，请稍后重试", "errors.status.serviceUnavailable 漂移（红线 R1，502/503/504）")
assert.equal(zhErrors.status.serverError, "服务器开小差了，请稍后重试", "errors.status.serverError 漂移（红线 R1，>=500）")
assert.equal(zhErrors.status.generic, "请求失败（{status}）", "errors.status.generic 漂移（红线 R1，client.ts statusFallbackMessage 非映射 status 兜底，{status} 占位符 verbatim）")
assert.equal(zhErrors.timeout, "请求超时（{seconds} 秒无响应），请检查网络后重试", "errors.timeout 漂移（红线 R1，client.ts timeout，{seconds} 占位符 verbatim）")
assert.equal(zhErrors.generic, "请求失败，请稍后重试。", "errors.generic 漂移（红线 R1，errors.ts getErrorMessage 兜底）")

// 9) UI-06 part2 W1（核心工作台 · 任务详情→结果/下载）：5 个 app* namespace（appWorkspace /
//    appResultMedia / appSmartPreviewConfirm / appSmartPreviewResult / appJianyingDraft）的内联中文
//    迁入字典后，默认 zh 值必须与改造前组件源【逐字节】相同（红线 R1）。下面钉死每个 namespace
//    标点/全半角/间隔号/破折号/箭头/占位符最敏感的代表串（任一漂移即 red = 默认 zh 渲染回归）：
//    - reviewNeeded 用全角冒号 ：(U+FF1A)；resynthesizing/processingStage 用间隔号 ·(U+00B7)
//    - editingInlineSuffix 用全角括号（）(U+FF08/U+FF09)；redirectToast 用半角三点 ...（历史，照搬不得改 …）
//    - completedBanner 是 next-intl rich-text <link> 标签（不得退回字面）；notification body 用 {title} 占位
//    - empty.loading.description 用全角省略号 …(U+2026)
const zhWorkspace = JSON.parse(readFileSync(path.join(root, "messages/zh/appWorkspace.json"), "utf8"))
assert.equal(zhWorkspace.reviewNeeded, "当前需要处理：{stage}", "appWorkspace.reviewNeeded 全角冒号/占位符漂移（红线 R1）")
assert.equal(zhWorkspace.resynthesizing, "正在重合成 · 第 {n} 次修改", "appWorkspace.resynthesizing 间隔号 ·/占位符漂移（红线 R1）")
assert.equal(zhWorkspace.processingStage, "正在处理 · {stage}", "appWorkspace.processingStage 间隔号 ·/占位符漂移（红线 R1）")
assert.equal(zhWorkspace.editingInlineSuffix, "（已完成 {n} 次修改）", "appWorkspace.editingInlineSuffix 全角括号/占位符漂移（红线 R1）")
assert.equal(zhWorkspace.redirectToast, "任务已完成，即将跳转到视频翻译主页...", "appWorkspace.redirectToast 半角三点 ... 漂移（红线 R1，照搬历史不得改全角 …）")
assert.equal(zhWorkspace.completedBanner, "任务已完成，请前往<link>视频翻译主页</link>查看和播放结果。", "appWorkspace.completedBanner rich-text <link> 标签/标点漂移（红线 R1）")
assert.equal(zhWorkspace.notification.succeeded.body, "{title} 已完成，点击查看结果", "appWorkspace.notification.succeeded.body {title} 占位符/标点漂移（红线 R1，title 是 content 透传）")
assert.equal(zhWorkspace.empty.loading.description, "正在加载工作区…", "appWorkspace.empty.loading.description 全角省略号 … 漂移（红线 R1）")

//    appResultMedia：material.subtitles 全角括号（中/英/双语）+ 半角斜杠；packFailed/packExpired 间隔号 ·；
//    packingPercent {pct}% 占位符 verbatim；packRetentionNote 全角分号 ；；generateDraftTitle 半角连字符 5-30
const zhResultMedia = JSON.parse(readFileSync(path.join(root, "messages/zh/appResultMedia.json"), "utf8"))
assert.equal(zhResultMedia.material.dubbed_video, "完整中文视频", "appResultMedia.material.dubbed_video 漂移（红线 R1）")
assert.equal(zhResultMedia.material.subtitles, "字幕包（中/英/双语）", "appResultMedia.material.subtitles 全角括号/半角斜杠漂移（红线 R1）")
assert.equal(zhResultMedia.packFailed, "打包失败 · 重试", "appResultMedia.packFailed 间隔号 · 漂移（红线 R1）")
assert.equal(zhResultMedia.packingPercent, "素材打包中 {pct}%", "appResultMedia.packingPercent {pct}% 占位符/空格漂移（红线 R1）")
assert.equal(zhResultMedia.packRetentionNote, "素材包仅保存 24 小时，请及时下载；超时后可重新打包，不额外扣点。", "appResultMedia.packRetentionNote 全角分号 ；/标点漂移（红线 R1）")
assert.equal(zhResultMedia.generateDraftTitle, "生成可用剪映打开的草稿包，5-30 秒", "appResultMedia.generateDraftTitle 半角连字符 5-30/全角逗号漂移（红线 R1）")
assert.equal(zhResultMedia.stage.starting, "正在准备", "appResultMedia.stage.starting 漂移（红线 R1，stageLabel）")

//    appSmartPreviewConfirm：title 间隔号 ·；credits {n} 点；insufficient 全角句号 。；consent 是声音权益
//    确认文案（W1 唯一 consent-adjacent 串）——钉死防漂移（counsel 完善前忠实翻译，逻辑/gate 不动）
const zhSpc = JSON.parse(readFileSync(path.join(root, "messages/zh/appSmartPreviewConfirm.json"), "utf8"))
assert.equal(zhSpc.title, "试用智能版 · 3 分钟预览", "appSmartPreviewConfirm.title 间隔号 · 漂移（红线 R1）")
assert.equal(zhSpc.credits, "{n} 点", "appSmartPreviewConfirm.credits {n} 占位符/空格漂移（红线 R1）")
assert.equal(zhSpc.insufficient, "余额不足 {cost}。", "appSmartPreviewConfirm.insufficient 全角句号 。/占位符漂移（红线 R1）")
assert.equal(zhSpc.consent, "我已了解本次预览将克隆主说话人音色并预扣 {cost}，且我拥有该视频的声音使用授权。", "appSmartPreviewConfirm.consent 声音权益文案漂移（红线 R1，consent 文案 counsel 前 verbatim）")

//    appSmartPreviewResult：header/badge 间隔号 ·；teaserNote 半角斜杠分隔 ' / '；convertingToast 全角省略号 …
const zhSpr = JSON.parse(readFileSync(path.join(root, "messages/zh/appSmartPreviewResult.json"), "utf8"))
assert.equal(zhSpr.header, "智能版 · 3 分钟预览", "appSmartPreviewResult.header 间隔号 · 漂移（红线 R1）")
assert.equal(zhSpr.badge, "带水印 · 仅在线播放", "appSmartPreviewResult.badge 间隔号 · 漂移（红线 R1）")
assert.equal(zhSpr.convertingToast, "正在转完整成片，按分钟正常扣点…", "appSmartPreviewResult.convertingToast 全角省略号 … 漂移（红线 R1）")
assert.equal(
  zhSpr.teaserNote,
  "这是用克隆音色生成的前 3 分钟带水印预览，仅供在线试看，不提供下载 / 导出 / 修改。满意后可转完整成片，去掉水印、生成全长内容。",
  "appSmartPreviewResult.teaserNote 半角斜杠分隔 ' / '/全角顿号 、漂移（红线 R1）",
)

//    appJianyingDraft：copied/copyAria {os} 占位；windowsLabel 全角冒号 ：；howToFindBody 箭头 → (U+2192)
const zhJd = JSON.parse(readFileSync(path.join(root, "messages/zh/appJianyingDraft.json"), "utf8"))
assert.equal(zhJd.copied, "已复制 {os} 路径", "appJianyingDraft.copied {os} 占位符/空格漂移（红线 R1，os 是 content 透传）")
assert.equal(zhJd.windowsLabel, "Windows 默认路径：", "appJianyingDraft.windowsLabel 全角冒号 ： 漂移（红线 R1）")
assert.equal(zhJd.howToFindBody, "打开剪映 → 设置 → 草稿位置，将该路径复制到上方输入框", "appJianyingDraft.howToFindBody 箭头 →/标点漂移（红线 R1）")
assert.equal(zhJd.placeholder, "请输入剪映草稿目录的绝对路径", "appJianyingDraft.placeholder 漂移（红线 R1）")

// 10) UI-06 part2 W2a（上传/提交表单 chrome）：appTranslationForm namespace 的内联中文迁入字典后，
//     默认 zh 值必须与改造前 TranslationForm/NewTranslationDialog/translations-new 源【逐字节】相同
//     （红线 R1）。consent/声音权益文案（《民法典》1023 等）**不在本片**——留 inline 待 W2b，故不 pin。
//     下面钉死标点/全半角/间隔号/箭头/占位符最敏感的代表串：
//     - credits/ratePerMin {n} 占位 + `点`/`点/分钟`；concurrency.limitReached 全角括号（）
//     - pricing.balance/toast.created/plan.freeQuota 全角冒号 ：；plan.free.desc 顿号 、+ ≤ + 全角（）
//     - plan.smart.previewDesc 全角括号（预扣 {cost}）；pricing.studio 半角斜杠 ' / ' + `点/分钟`
//     - smartPause.body 全角弯引号 “弱匹配确认”；advanced.gemini `≤30分钟`（无空格）；submit.submitting 全角省略号 …
//     - pricing.free 全角直角引号「」+ ascii `add-on`；upload.merging 全角省略号 …+全角括号（）
const zhTf = JSON.parse(readFileSync(path.join(root, "messages/zh/appTranslationForm.json"), "utf8"))
assert.equal(zhTf.credits, "{n} 点", "appTranslationForm.credits {n}/`点` 漂移（红线 R1）")
assert.equal(zhTf.ratePerMin, "{n} 点/分钟", "appTranslationForm.ratePerMin {n}/`点/分钟` 漂移（红线 R1）")
assert.equal(zhTf.loading, "读取中", "appTranslationForm.loading 漂移（红线 R1，注意无省略号，与 W1 appSmartPreviewConfirm.loading 的 读取中… 不同源）")
assert.equal(zhTf.concurrency.limitReached, "已达到并发上限（{count}/{limit}）", "appTranslationForm.concurrency.limitReached 全角括号/占位符漂移（红线 R1）")
assert.equal(zhTf.pricing.balance, "当前可用：{balance}", "appTranslationForm.pricing.balance 全角冒号/占位符漂移（红线 R1）")
assert.equal(zhTf.toast.created, "任务已创建：{title}", "appTranslationForm.toast.created 全角冒号/占位符漂移（红线 R1，title 是 content 透传）")
assert.equal(zhTf.plan.freeQuota, "免费额度：已用 {used} / {total} 次", "appTranslationForm.plan.freeQuota 全角冒号/半角斜杠/占位符漂移（红线 R1）")
assert.equal(
  zhTf.plan.express.desc,
  "全自动流程，AI 识别说话人、翻译、配音，无需人工操作。",
  "appTranslationForm.plan.express.desc 顿号 、/标点漂移（红线 R1）",
)
assert.equal(
  zhTf.plan.free.desc,
  "免费保留原声 AI 配音（限时），每日 1 次、单条 ≤10 分钟，成品带水印。",
  "appTranslationForm.plan.free.desc 全角括号/顿号/≤/空格漂移（红线 R1）",
)
assert.equal(
  zhTf.plan.smart.desc,
  "100 点/分钟固定价，AI 自动审核翻译并自动克隆音色，无需人工操作。",
  "appTranslationForm.plan.smart.desc `点/分钟`/标点漂移（红线 R1）",
)
assert.equal(
  zhTf.plan.smart.previewDesc,
  "克隆主说话人音色，先看前 3 分钟带水印预览（预扣 {cost}）。满意再转完整成片，按分钟正常扣点。",
  "appTranslationForm.plan.smart.previewDesc 全角括号/占位符漂移（红线 R1）",
)
assert.equal(
  zhTf.pricing.studio,
  "工作台版按源视频时长扣点，基础标准为 {rate}；后续选择高级/旗舰音质时分别按 {high} / {flagship} 点/分钟扣除。音色克隆为单次独立扣点，克隆弹窗会再次确认费用。",
  "appTranslationForm.pricing.studio 半角斜杠 ' / '/`点/分钟`/全角分号/占位符漂移（红线 R1）",
)
assert.equal(
  zhTf.pricing.free,
  "免费版当前不扣点（限时免费），保留原声 AI 配音。每日 1 次、单条 ≤10 分钟，成品视频带水印。后续如需「后编辑」或「剪映草稿」为付费 add-on，将另行计点。",
  "appTranslationForm.pricing.free 全角直角引号「」/add-on/标点漂移（红线 R1）",
)
assert.equal(
  zhTf.smartPause.body,
  "管理员已开启“弱匹配确认”策略：如果系统在你的个人音色库中发现可能匹配的音色（但相似度不够强），任务会暂停在音色审核页面，等你确认是否复用，再继续后续步骤。复用个人音色不消耗克隆点；如果不想复用，可以选择官方音色或重新克隆。",
  "appTranslationForm.smartPause.body 全角弯引号 “”/标点漂移（红线 R1）",
)
assert.equal(zhTf.advanced.gemini, "Gemini 多模态（≤30分钟）", "appTranslationForm.advanced.gemini 全角括号/≤/无空格 漂移（红线 R1）")
assert.equal(zhTf.advanced.speakerCount, "{n} 人", "appTranslationForm.advanced.speakerCount {n}/`人` 漂移（红线 R1）")
assert.equal(zhTf.language.beta, "（内测）", "appTranslationForm.language.beta 全角括号漂移（红线 R1）")
assert.equal(zhTf.submit.submitting, "创建中…", "appTranslationForm.submit.submitting 全角省略号 … 漂移（红线 R1）")
assert.equal(
  zhTf.upload.merging,
  "正在合并校验…（大文件需要数十秒，请勿关闭页面）",
  "appTranslationForm.upload.merging 全角省略号/全角括号漂移（红线 R1）",
)
assert.equal(zhTf.upload.failed, "上传失败", "appTranslationForm.upload.failed 漂移（红线 R1）")

// 11) UI-06 part2 W2b（consent/法务文案）：appTranslationFormConsent namespace 的内联中文迁入字典后，
//     默认 zh 值必须与改造前 TranslationForm 源【逐字节】相同（红线 R1）。这是**法务/声音授权文案**
//     （《民法典》1023 等），漂移即默认 zh 法务渲染回归——逐条钉死（全部 pin，不抽样）。en 为 Claude
//     把关的忠实翻译（疑点见 PR），逻辑/门控不在本守卫范围（不变）。
const zhTfc = JSON.parse(readFileSync(path.join(root, "messages/zh/appTranslationFormConsent.json"), "utf8"))
assert.equal(
  zhTfc.youtubeRightsHint,
  "仅用于翻译您本人或已获授权的视频内容；使用前请确认拥有合法授权，不得用于侵权用途。",
  "appTranslationFormConsent.youtubeRightsHint 全角分号/标点漂移（红线 R1，法务）",
)
assert.equal(zhTfc.free.title, "声音授权声明（必读必勾）", "appTranslationFormConsent.free.title 全角括号漂移（红线 R1，法务）")
assert.equal(
  zhTfc.free.attestation,
  "我确认：我已获得该视频内容及其中所有说话人声音的合法授权，或该使用属于法律允许的范围；因使用本服务声音克隆功能产生的肖像权 / 声音权纠纷由我自行承担。",
  "appTranslationFormConsent.free.attestation 《民法典》1023 声音授权 attestation 漂移（红线 R1，法务核心，全角：，；/半角斜杠/责任转移措辞 verbatim）",
)
assert.equal(zhTfc.free.validation, "请先阅读并勾选免费版声音授权声明。", "appTranslationFormConsent.free.validation 漂移（红线 R1，法务）")
assert.equal(zhTfc.express.title, "自动克隆主说话人音色", "appTranslationFormConsent.express.title 漂移（红线 R1，克隆 opt-in）")
assert.equal(zhTfc.express.experimental, "实验性", "appTranslationFormConsent.express.experimental 漂移（红线 R1）")
assert.equal(
  zhTfc.express.desc,
  "勾选后，系统会用视频中占比最高的说话人的一小段语音（约 10–20 秒）克隆一个临时音色用于本次配音，让主说话人的声音更贴近原片。",
  "appTranslationFormConsent.express.desc en-dash 10–20/全角括号/标点漂移（红线 R1，克隆 opt-in）",
)
assert.equal(
  zhTfc.express.bullet1,
  "· 该音色为本次任务临时使用，不进入你的永久音色库；系统后续会按清理策略处理",
  "appTranslationFormConsent.express.bullet1 间隔号 ·/全角分号 ；漂移（红线 R1）",
)
assert.equal(zhTfc.express.bullet2, "· 会占用一次音色克隆配额", "appTranslationFormConsent.express.bullet2 间隔号 · 漂移（红线 R1）")
assert.equal(zhTfc.express.bullet3, "· 失败时自动改用预设音色，不影响配音完成", "appTranslationFormConsent.express.bullet3 间隔号 · 漂移（红线 R1）")
assert.equal(
  zhTfc.express.validation,
  "快捷版 CosyVoice 需要先确认自动克隆主说话人音色。",
  "appTranslationFormConsent.express.validation 漂移（红线 R1，克隆 opt-in 校验）",
)
assert.equal(zhTfc.smart.title, "确认智能版自动克隆扣点", "appTranslationFormConsent.smart.title 漂移（红线 R1，付费克隆 consent）")
assert.equal(
  zhTfc.smart.attestation,
  "我确认：如果本次智能版需要自动新克隆主说话人音色，将额外预扣 {cost}；未发生新克隆或任务未消耗该克隆时会释放。",
  "appTranslationFormConsent.smart.attestation 全角：/；{cost} 占位符漂移（红线 R1，付费克隆 attestation）",
)

// 12) UI-06 part2 W3a（Studio 审校面板）：appTranslationReview / appVoiceReview /
//     appSmartAutoDecision 三 namespace 的内联中文迁入字典后，默认 zh 值必须与改造前组件源
//     【逐字节】相同（红线 R1）。钉死标点/全半角/特殊符号（✕ U+2715 / ▶ U+25B6 / 、U+3001 /
//     … U+2026 / 全角 ：（） / 半角冒号空格）/占位符最敏感的代表串：
const zhVr = JSON.parse(readFileSync(path.join(root, "messages/zh/appVoiceReview.json"), "utf8"))
assert.equal(zhVr.desc, "请为每个说话人选择豆包 2.0 音色，或选择「自动匹配」由系统根据说话人特征自动选择。", "appVoiceReview.desc 全角直角引号「」/标点漂移（红线 R1）")
assert.equal(zhVr.selectForSpeakers, "请为以下说话人选择音色或\"自动匹配\"：{names}", "appVoiceReview.selectForSpeakers 半角双引号/全角冒号/占位符漂移（红线 R1）")
assert.equal(zhVr.autoMatch, "自动匹配（系统根据说话人特征选择）", "appVoiceReview.autoMatch 全角括号漂移（红线 R1）")
assert.equal(zhVr.submitFailed, "音色确认失败: {msg}", "appVoiceReview.submitFailed 半角冒号/占位符漂移（红线 R1）")

const zhTrv = JSON.parse(readFileSync(path.join(root, "messages/zh/appTranslationReview.json"), "utf8"))
assert.equal(zhTrv.confirmHint, "确认翻译与配音文本，共 {count} 条。", "appTranslationReview.confirmHint 占位符/标点漂移（红线 R1）")
assert.equal(zhTrv.cancelSplit, "✕ 取消拆分", "appTranslationReview.cancelSplit ✕ U+2715 漂移（红线 R1）")
assert.equal(zhTrv.playSource, "▶ 播放原文", "appTranslationReview.playSource ▶ U+25B6 漂移（红线 R1）")
assert.equal(zhTrv.segmentLabel, "片段 {id}", "appTranslationReview.segmentLabel 占位符/空格漂移（红线 R1，id 是 content）")
assert.equal(zhTrv.splitSourcePos, "原文拆分位置（{pos}）", "appTranslationReview.splitSourcePos 全角括号/占位符漂移（红线 R1）")
assert.equal(zhTrv.splitFailed, "拆分失败: {msg}", "appTranslationReview.splitFailed 半角冒号/占位符漂移（红线 R1）")
assert.equal(zhTrv.pageInfo, "第 {current} / {total} 页", "appTranslationReview.pageInfo 占位符/半角斜杠/空格漂移（红线 R1）")
assert.equal(zhTrv.showRange, "显示 {from}-{to} / {total}", "appTranslationReview.showRange 占位符/半角斜杠/连字符漂移（红线 R1）")

const zhSad = JSON.parse(readFileSync(path.join(root, "messages/zh/appSmartAutoDecision.json"), "utf8"))
assert.equal(zhSad.loading, "正在加载智能版决策摘要…", "appSmartAutoDecision.loading 全角省略号 … 漂移（红线 R1）")
assert.equal(zhSad.loadFailed, "智能版决策摘要加载失败：{msg}", "appSmartAutoDecision.loadFailed 全角冒号/占位符漂移（红线 R1）")
assert.equal(zhSad.billingPolicy, "计费策略：", "appSmartAutoDecision.billingPolicy 全角冒号漂移（红线 R1）")
assert.equal(zhSad.listSeparator, "、", "appSmartAutoDecision.listSeparator 顿号 、U+3001 漂移（红线 R1，join 分隔符）")
assert.equal(zhSad.excludedItem, "{id}（{reason}）", "appSmartAutoDecision.excludedItem 全角括号/占位符漂移（红线 R1）")
assert.equal(zhSad.voiceId, "音色 ID", "appSmartAutoDecision.voiceId 空格漂移（红线 R1）")
assert.equal(zhSad.notApproved, "未通过：{check}", "appSmartAutoDecision.notApproved 全角冒号/占位符漂移（红线 R1）")
assert.equal(zhSad.status.completed, "已完成", "appSmartAutoDecision.status.completed 漂移（红线 R1）")
assert.equal(zhSad.status.downgraded, "已转人工", "appSmartAutoDecision.status.downgraded 漂移（红线 R1）")
assert.equal(zhSad.status.refunded, "已退款", "appSmartAutoDecision.status.refunded 漂移（红线 R1）")
assert.equal(zhSad.policy.full, "正常计费", "appSmartAutoDecision.policy.full 漂移（红线 R1）")
assert.equal(zhSad.minutes, "分钟", "appSmartAutoDecision.minutes 漂移（红线 R1）")

// 13) UI-06 part2 W3b（Studio 音色选择 + 说话人管理）：appVoiceSelection / appSpeakerAudit /
//     appSpeakerCreate / appSpeakerBadge 四 namespace 的内联中文迁入字典后，默认 zh 值必须与改造前
//     组件源【逐字节】相同（红线 R1）。钉死跨模板拼接（warnSpeaker）、特殊符号（★ U+2605 / 🎯 /
//     em-dash — / 「」U+300C-D）、全角括号（）、占位符最敏感的代表串：
const zhVs = JSON.parse(readFileSync(path.join(root, "messages/zh/appVoiceSelection.json"), "utf8"))
assert.equal(
  zhVs.desc,
  "请为每位说话人选择预设音色或克隆专属音色，确认后继续生成配音。",
  "appVoiceSelection.desc 漂移（红线 R1）",
)
assert.equal(
  zhVs.expiredBanner,
  "检测到 {count} 个音色已失效，已从选项中移除。请重新选择音色。",
  "appVoiceSelection.expiredBanner 占位符/标点漂移（红线 R1）",
)
assert.equal(zhVs.optionLabelCps, "{base} · {cps}字/秒({tier})", "appVoiceSelection.optionLabelCps 间隔号 ·/半角括号/占位符漂移（红线 R1）")
assert.equal(zhVs.badge.strong, "★ 强匹配", "appVoiceSelection.badge.strong ★ U+2605 漂移（红线 R1）")
assert.equal(zhVs.segDuration, "{count} 段 · {dur}s", "appVoiceSelection.segDuration 间隔号 ·/占位符/空格漂移（红线 R1）")
assert.equal(zhVs.optgroup.smartRec, "🎯 智能推荐 (按匹配度排序)", "appVoiceSelection.optgroup.smartRec 🎯/半角括号漂移（红线 R1）")
assert.equal(zhVs.creditsPerMinute, "{credits} 点/分钟", "appVoiceSelection.creditsPerMinute 半角斜杠/占位符漂移（红线 R1）")
assert.equal(zhVs.configuredCount, "{done} / {total} 说话人已配置音色", "appVoiceSelection.configuredCount 占位符/半角斜杠/空格漂移（红线 R1）")
// warnSpeaker：原为 3 段模板字面量 + 拼接（L699-702），迁为单 ICU 后【渲染逐字节相同】。
assert.equal(
  zhVs.warnSpeaker,
  "{name}：选定音色 {voiceCps} 字/秒，比原说话人需要的 {targetCps} 字/秒{fast} {pct}%，配音可能需要大幅{direction}。",
  "appVoiceSelection.warnSpeaker 跨模板拼接 ICU/全角：，/占位符漂移（红线 R1，渲染须与原三段拼接逐字节一致）",
)

const zhSpa = JSON.parse(readFileSync(path.join(root, "messages/zh/appSpeakerAudit.json"), "utf8"))
assert.equal(zhSpa.title, "核对原音 —", "appSpeakerAudit.title em-dash — 漂移（红线 R1，标题拼接说话人名）")
assert.equal(zhSpa.countPrefix, "共", "appSpeakerAudit.countPrefix 漂移（红线 R1，nested span 前缀）")
assert.equal(zhSpa.countSuffix, "段", "appSpeakerAudit.countSuffix 漂移（红线 R1，nested span 后缀）")
assert.equal(zhSpa.segmentFallback, "片段 {id}", "appSpeakerAudit.segmentFallback 占位符/空格漂移（红线 R1，id 是 content）")
assert.equal(
  zhSpa.readOnlyHint,
  "试听原音以核对说话人归属。需修改归属或保留原音请到「翻译修改」Tab 在段落上操作。",
  "appSpeakerAudit.readOnlyHint 全角直角引号「」/句号漂移（红线 R1）",
)

const zhScr = JSON.parse(readFileSync(path.join(root, "messages/zh/appSpeakerCreate.json"), "utf8"))
assert.equal(
  zhScr.description,
  "为 S2 漏检的说话人新建一个条目。创建后请到段落下拉里把属于这个说话人的段都改归属， 后台会自动跑一次音色画像推断（约 5-15 秒）。",
  "appSpeakerCreate.description JSX 折叠空格/全角括号（约 …）/半角连字符 5-15 漂移（红线 R1）",
)
assert.equal(zhScr.placeholder, "例：桑达尔·皮查伊", "appSpeakerCreate.placeholder 全角冒号：/间隔号 ·（人名示例，content）漂移（红线 R1）")
assert.equal(zhScr.nameConflictServer, "已存在同名说话人，请改一个名字", "appSpeakerCreate.nameConflictServer 全角逗号漂移（红线 R1）")

const zhSpb = JSON.parse(readFileSync(path.join(root, "messages/zh/appSpeakerBadge.json"), "utf8"))
assert.equal(zhSpb.status.inferring, "音色画像推断中...", "appSpeakerBadge.status.inferring 半角三点漂移（红线 R1）")
assert.equal(zhSpb.status.ready, "音色画像就绪", "appSpeakerBadge.status.ready 漂移（红线 R1）")
assert.equal(zhSpb.retry, "重试", "appSpeakerBadge.retry 漂移（红线 R1）")

// 14) UI-06 part2 W4a（音色克隆 modal 三件套）：appCosyClone / appVoiceClone /
//     appCosySegments 三 namespace 的内联中文迁入字典后，默认 zh 值必须与改造前
//     组件源【逐字节】相同（红线 R1）。appVoiceClone 是 MiniMax 旧 clone modal
//     的命名空间（G6.1.5 / G_MX.2 守卫要求该文件永不含 "cosyvoice" 字面量，
//     namespace 名与 key/value 均不得出现 cosyvoice）。钉死标点/全半角/间隔号/
//     em-dash/直角引号/占位符最敏感的代表串：
const zhCc = JSON.parse(readFileSync(path.join(root, "messages/zh/appCosyClone.json"), "utf8"))
assert.equal(zhCc.targetModel.flash.label, "Flash（推荐）", "appCosyClone.targetModel.flash.label 全角括号漂移（红线 R1）")
assert.equal(
  zhCc.targetModel.flash.description,
  "DashScope cosyvoice-v3.5-flash · 国际端点延迟低 · ¥0.01/次",
  "appCosyClone.targetModel.flash.description 间隔号 ·/¥ 漂移（红线 R1）",
)
assert.equal(zhCc.dialogTitle, "克隆「{speakerName}」的声音", "appCosyClone.dialogTitle 全角直角引号「」/占位符漂移（红线 R1）")
assert.equal(
  zhCc.dialogDescription,
  "CosyVoice 克隆音色后会出现在你的个人音色库，可在后续任务中复用。",
  "appCosyClone.dialogDescription 漂移（红线 R1）",
)
assert.equal(zhCc.fileErrorTooLarge, "文件超过 10MB 上限（当前 {size}MB）", "appCosyClone.fileErrorTooLarge 全角括号/占位符漂移（红线 R1）")
assert.equal(zhCc.selectedFile, "已选择：{name} ({size} KB)", "appCosyClone.selectedFile 全角冒号/占位符漂移（红线 R1）")
assert.equal(
  zhCc.uploadFileHint,
  "WAV (PCM 16-bit) / MP3 / M4A · 3-60 秒 · ≤10 MB · ≥16 kHz · 本人清晰朗读，无背景音乐 / 多人声",
  "appCosyClone.uploadFileHint 间隔号 ·/≤/≥漂移（红线 R1，JSX 折叠空格渲染须逐字节一致）",
)
assert.equal(zhCc.cancel, "取消", "appCosyClone.cancel 漂移（红线 R1）")
assert.equal(zhCc.errorCode.forbiddenNotInAllowlist, "当前账号未在 CosyVoice 克隆灰度名单中", "appCosyClone.errorCode.forbiddenNotInAllowlist 漂移（红线 R1）")

const zhVc = JSON.parse(readFileSync(path.join(root, "messages/zh/appVoiceClone.json"), "utf8"))
assert.equal(zhVc.reuseConfidence.strong, "同一视频 / 同一说话人", "appVoiceClone.reuseConfidence.strong 半角斜杠漂移（红线 R1）")
assert.equal(zhVc.title, "克隆音色 — {speakerName}", "appVoiceClone.title em-dash —/占位符漂移（红线 R1）")
assert.equal(zhVc.foundReusableVoice, "发现可复用音色：{label}", "appVoiceClone.foundReusableVoice 全角冒号/占位符漂移（红线 R1）")
assert.equal(zhVc.originalSampleSuffix, " · 原样本 {seconds}", "appVoiceClone.originalSampleSuffix 间隔号 ·/前导空格/占位符漂移（红线 R1）")
assert.equal(zhVc.autoSelectHint, "从最长片段开始自动勾选，总时长 < 300s", "appVoiceClone.autoSelectHint 漂移（红线 R1，&lt; JSX 实体渲染为字面 <）")
assert.equal(zhVc.selectedCountPrefix, "已选", "appVoiceClone.selectedCountPrefix 漂移（红线 R1，nested span 前缀）")
assert.equal(zhVc.selectedCountSuffix, "段", "appVoiceClone.selectedCountSuffix 漂移（红线 R1，nested span 后缀）")
assert.equal(zhVc.segmentFallback, "片段 {id}", "appVoiceClone.segmentFallback 占位符/空格漂移（红线 R1）")
assert.equal(zhVc.recloneCostCredits, "重新克隆会消耗 {credits} 点", "appVoiceClone.recloneCostCredits 占位符/空格漂移（红线 R1）")
assert.equal(zhVc.cloneCostCredits, "克隆费用：{credits} 点", "appVoiceClone.cloneCostCredits 全角冒号/占位符漂移（红线 R1）")
// G6.1.5 / G_MX.2 守卫要求 VoiceCloneModal.tsx（appVoiceClone 消费方）永不含
// "cosyvoice" 字面量；镜像校验 namespace 字典本身也不得意外引入该字面量。
assert.ok(
  !JSON.stringify(zhVc).toLowerCase().includes("cosyvoice"),
  "appVoiceClone.json 不得含 'cosyvoice' 字面量（G6.1.5 / G_MX.2 MiniMax modal 隔离红线）",
)

const zhCs = JSON.parse(readFileSync(path.join(root, "messages/zh/appCosySegments.json"), "utf8"))
assert.equal(zhCs.tooShort, "还需 {needSec}s 才能克隆（最少 {min}s）", "appCosySegments.tooShort 全角括号/占位符漂移（红线 R1）")
assert.equal(zhCs.tooLong, "已超出 {overSec}s（最多 {max}s）", "appCosySegments.tooLong 全角括号/占位符漂移（红线 R1）")
assert.equal(
  zhCs.emptyHint,
  "勾选 {min}-{max} 秒的段作为 克隆样本（推荐 {recMin}-{recMax} 秒效果最好）",
  "appCosySegments.emptyHint 全角括号/占位符/JSX 折叠空格漂移（红线 R1，渲染须与原多行 JSX 逐字节一致）",
)
assert.equal(zhCs.okStatus, "✓ 已选 {count} 段 · 共 {sec}s", "appCosySegments.okStatus ✓ U+2713/间隔号 ·/占位符漂移（红线 R1）")
assert.equal(zhCs.okStatusRecommendSuffix, " · 建议落到 {recMin}-{recMax}s", "appCosySegments.okStatusRecommendSuffix 间隔号 ·/前导空格/占位符漂移（红线 R1）")
assert.equal(zhCs.loadErrorHint, "可改用「上传音频文件」模式，或稍后重试。", "appCosySegments.loadErrorHint 全角直角引号「」漂移（红线 R1）")
assert.equal(zhCs.shortSingleHint, "单段较短，建议组合多段拼到 3 秒以上", "appCosySegments.shortSingleHint 漂移（红线 R1）")

// 15) UI-06 part2 W4b（CosyVoice 克隆授权 consent modal）：appCosyConsent namespace 的内联中文
//     迁入字典后，默认 zh 值必须与改造前 CosyVoiceConsentModal 源【逐字节】相同（红线 R1）。这是
//     **声纹生物特征法务 consent**（version-pinned modal_version="2026-05-25-v1"），漂移即默认 zh
//     法务渲染回归——**逐条钉死全部 13 串**（不抽样）。en 为 Claude 把关的忠实翻译（法务疑点见 PR：
//     en consent 挂 zh-anchored modal_version）；modal_version / CONSENT_MODAL_VERSION 逻辑不在本守卫
//     范围（未动，phase42 D2 守卫另测）。
const zhCon = JSON.parse(readFileSync(path.join(root, "messages/zh/appCosyConsent.json"), "utf8"))
assert.equal(zhCon.title, "声音克隆授权确认", "appCosyConsent.title 漂移（红线 R1，法务）")
assert.equal(
  zhCon.description,
  "为了创建您专属的克隆音色，我们需要您本人的声音样本。在继续之前， 请确认以下三项内容并勾选。任一未勾选时，「开始克隆」按钮将保持禁用。",
  "appCosyConsent.description JSX 折叠空格/全角直角引号「」漂移（红线 R1，法务）",
)
assert.equal(
  zhCon.checkbox.source.title,
  "我确认：本次提供的声音样本是我本人的声音，或者我已获得声音所有人的书面授权用于声音克隆和后续 TTS 合成。",
  "appCosyConsent.checkbox.source.title 声音授权 attestation 漂移（红线 R1，法务核心）",
)
assert.equal(
  zhCon.checkbox.source.detail,
  "我理解：未经声音所有人明确同意而克隆他人声音（含名人、公众人物、家人、朋友）属于侵权行为，本平台有权随时停用该音色并保留追究法律责任的权利。",
  "appCosyConsent.checkbox.source.detail 侵权/法律责任措辞漂移（红线 R1，法务核心）",
)
assert.equal(
  zhCon.checkbox.data_flow.title,
  "我同意：声音样本将上传至中国境内的阿里云语音合成服务进行处理，用于生成与我声音相似的合成音色。",
  "appCosyConsent.checkbox.data_flow.title 数据流向 consent 漂移（红线 R1，法务核心）",
)
assert.equal(
  zhCon.checkbox.data_flow.detail,
  "样本数据：用途仅用于本次克隆音色生成与后续 TTS 合成；处理位置为中国境内的阿里云服务器（2026-05 实测以「华北 2 - 北京」为主，本平台不固定单一 region）；中转节点位于中国境内武汉；克隆完成后 24 小时内删除原始样本，克隆出的 voice_id 保留在个人音色库由用户自主管理；本平台不会主动将样本用于训练通用 AI 模型，也不会主动与无关第三方共享或用于商业广告投放；阿里云作为第三方处理方，其对样本数据的具体处理规则以阿里云服务条款 / 数据处理协议为准。",
  "appCosyConsent.checkbox.data_flow.detail 数据处理披露（阿里云/华北2-北京/武汉/24h 删除/全角；分号）漂移（红线 R1，法务核心）",
)
assert.equal(
  zhCon.checkbox.consequences.title,
  "我了解并同意：违规后果与退出权 —— 平台收到投诉时音色将被停用、调查；冒用他人声音可能导致账号封禁。",
  "appCosyConsent.checkbox.consequences.title em-dash ——/全角标点漂移（红线 R1，法务核心）",
)
assert.equal(
  zhCon.checkbox.consequences.detail,
  "如平台收到声音所有人投诉或第三方举报，我的克隆音色将被立即停用、调查；调查期间相关功能可能暂停。如确认存在冒用他人声音的行为，本平台有权封禁我的账号，并依法配合相关部门调查；由此产生的法律责任由我本人承担。我可随时在「个人音色库」页面删除已克隆的音色；删除后音色不可用于新的 TTS 合成，已合成的历史视频不受影响。",
  "appCosyConsent.checkbox.consequences.detail 违规后果/退出权全文漂移（红线 R1，法务核心）",
)
assert.equal(zhCon.paidApiLabel, "付费 API 提示：", "appCosyConsent.paidApiLabel 全角冒号漂移（红线 R1）")
assert.equal(
  zhCon.paidApiBody,
  "点击「开始克隆」后将向 DashScope CosyVoice 发起一次音色克隆请求 （每次约 ¥0.01 + ¥0.005 试听）。失败不重试 — 重试需您主动再次提交。",
  "appCosyConsent.paidApiBody 付费提示（¥0.01/¥0.005/全角括号/破折号 —）漂移（红线 R1）",
)
assert.equal(zhCon.cancel, "取消", "appCosyConsent.cancel 漂移（红线 R1）")
assert.equal(zhCon.startClone, "开始克隆", "appCosyConsent.startClone 漂移（红线 R1）")
assert.equal(zhCon.confirmAllTip, "请先确认上述全部三项条款", "appCosyConsent.confirmAllTip 漂移（红线 R1）")

console.log("[zh-snapshot] OK — 默认 zh 不变量 + site.ts inert + auth/marketing/seo/app/errors/工作台 W1+W2a+W2b+W3a+W3b+W4a+W4b 字节一致 + en seo 双源同步 全部通过")
