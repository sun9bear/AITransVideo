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

console.log("[zh-snapshot] OK — 默认 zh 不变量 + site.ts inert + auth/marketing/seo 字节一致 + en seo 双源同步 全部通过")
