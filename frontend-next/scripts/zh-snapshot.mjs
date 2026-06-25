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

const hl = site.hreflangLanguages("/")
assert.deepEqual(
  hl,
  { "zh-Hans": SITE_URL, "x-default": SITE_URL },
  "hreflang 非 inert（UI-01 应只含 zh-Hans + x-default，均指 zh）"
)

// en 分支声明可用（未被消费）：前缀正确，供 UI-03 翻旗
assert.equal(site.absoluteUrl("/pricing", "en"), `${SITE_URL}/en/pricing`, "absoluteUrl en 前缀错误")

// 3) auth 默认 zh 字节一致（UI-04 红线 1）：/auth · /auth/login · /auth/register ·
//    /auth/forgot-password 四页 + 三表单 + captcha 的串迁入 messages/zh/auth.json 后，
//    zh 值必须与改造前的内联字面量【逐字节】相同。下面钉死最易漂的标点敏感串：
//    - phone-login-form 用【半角逗号 ,】+【全角省略号 …】
//    - email-register-form 用【全角逗号 ，】+【半角三点 ...】
//    （二者历史不一致——照搬，不得"修正"。改任一值即在此处 red。）
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

console.log("[zh-snapshot] OK — 默认 zh 不变量 + site.ts inert + auth 字节一致 全部通过")
