# UI-04 · 最小 Auth 英文化（防漏斗断）

| 字段 | 内容 |
| --- | --- |
| 目标 | 把登录 / 注册 / 找回密码这条「营销 CTA 落地」流英文化到 `auth` namespace，并让验证码控件（GeeTest/Turnstile）跟随界面 locale，使 `/en` 营销页的「免费试用 / 登录 / 注册」CTA 点进去后不再撞中文表单。 |
| 价值 | 英文获客漏斗在注册口不断（CodeX v2 前移本阶段的全部理由）。这是 M1（P0a+P0b+Phase 1+Phase 1.5）里与营销同一个里程碑、必须一起上的最小 auth 面。 |
| 关联 | 主方案 `docs/plans/2026-06-25-ui-page-locale-switch-plan.md` → **Phase 1.5 = Task 1.5.1 / 1.5.2 / 1.5.3 / 1.5.4**；强相关 §1.9（server-emitted 文本边界）、§0.3 红线、§1.4（ICU + namespace 切片）、§3 阶段表 Phase 1.5 行、§7 DoD「Phase 1.5」行、§8 风险 R4。 |
| 前置依赖 | **UI-02**（routing：`[locale]` 段 + `proxy` 合并 + 本地化 `navigation` 已落地，否则 `auth.json` 无 locale 来源、`<Link>` 丢前缀）；**UI-01**（catalog：`messages/{zh,en}/*.json` 目录骨架 + `i18n/request.ts` 具体 import + `IntlMessages` 类型 + `getTranslations`/`useTranslations` 可用）。 |
| 建议分支 | `uiloc/min-auth-en` |
| 预估工时 | **M**（一次 dedupe + 4 表单 + 1 找回页 + captcha locale + 客户端兜底集中；无后端、无路由结构改动） |

---

> **⚠️ 路径约定（UI-02 之后，CodeX 二审 #2）**：本单元在 **UI-02 结构迁移之后**执行——`(auth)` 已位于 **`app/[locale]/(auth)/`**。下文为可读性仍写 `app/(auth)/...`，**一律理解为迁移后的 `app/[locale]/(auth)/...`**；Step 0 必须先以 `[locale]` 真实路径重新核对所有 file 锚点与 grep 目标（`(auth)` 不再在 `app/` 顶层）。

## 不在本单元范围（out-of-scope）

- **共享 UI primitives + 完整错误层**：`confirm-dialog` / `empty-state` / `log-viewer` 默认 props、shadcn a11y 串、`session-provider` 内联错误、`components/support/**` —— 全在 **Phase 3 / UI-08 / UI-09**，本单元不碰。
- **`lib/api/client.ts` 与 `lib/api/errors.ts` 的「集中错误层重构」**：本单元**只**把这两处现有中文兜底字面量参数化为可本地化串（auth 流会经过它们），**不**做 Phase 3 Task 3.4 的「单一可本地化错误模块 + 全站 error-code map 推广」。只动 auth 流真正会命中的兜底文案，不重排错误层架构。
- **workspace / 工作台 / app 中央字典**（`JOB_STATUS_LABELS` / `presentation.ts` / `stageMetadata` / `TranslationForm` 等）→ Phase 2 / UI-05+。
- **admin（`app/(app)/admin/**`）** → 永久 OUT OF SCOPE（保持中文）。
- **后端 server-emitted 中文**（gateway `detail="<中文>"`、邮件、客服）→ **Phase 4 独立后端轨**。本单元**只 mask 客户端兜底**，服务端 `detail` 仍是中文（见「必守不变量」与 DoD 缺口声明）。
- **`captcha-gate.tsx` 的 provider 切换 / 安全逻辑**：只把硬编码的 `language: 'zho'` / `language: 'zh-cn'` 与内联中文状态串改成 locale 驱动，**不动** token 解析、pending/timeout、provider dispatch。
- **路由结构 / `proxy` / `<html lang>`**（UI-02 已做）；**营销页文案 / SEO**（Phase 1 / UI-03）。

---

## 必守不变量

只列与本单元相关的红线（出处：主方案 §0.3 + §1.2a + §1.5 + §1.9）：

1. **zh 默认字节一致**（红线 1）：迁移后默认中文（`/auth` `/auth/login` `/auth/register` `/auth/forgot-password` 的 DOM 文本）必须与改造前**逐字节相同**。dedupe 不得改变 zh 渲染输出（含标点：注意现有串混用了半角逗号 `,` 与全角逗号 `，`，见 phone-login-form 与 email-register-form——**照搬到 zh 字典，不要顺手"修正"**）。
2. **presentation-only，不碰付费 API**（红线 3）：本单元纯表现层，**不得**新增/改变任何 TTS/clone/LLM/ASR 付费路径，不改任何 `fetch` 的 endpoint / payload / method。只换**展示字符串**，不动网络契约。
3. **UI locale 不碰 pipeline 语言字段**（§0.1 硬不变量）：不得触碰 `source_language` / `target_language` / `language_pair` / `cn_text`。auth 流本就与这些无关——保持无关。
4. **content 永不翻译**（红线 5）：用户输入的手机号 / 邮箱回显（`{phone}` / `{normalizedEmail}`）是 content，只翻**周边 chrome**（`已向 … 发送验证码` 模板的固定部分），回显值 verbatim 透传。
5. **`<html lang>` 由 SSR 在 `[locale]/layout` 设**（§1.5）：本单元**不得**用 client effect 改 lang；captcha widget 的 `language` 参数从 active locale **读取**（`useLocale()`），不另立一套 lang 真源。
6. **ICU，不字符串拼接**（§1.4）：倒计时 `{n}s 后可重发`、手机号回显 `已向 {phone} 发送验证码`、秒数/位数（`至少 {n} 位` / `请求超时（{n} 秒）`）一律 ICU message，**禁** `` `${n}s 后...` `` 模板拼接（英文复数/语序会破）。
7. **server detail 仍是中文（已知缺口）**（§1.9 / R4）：本单元**只**本地化 `data?.detail || "<中文兜底>"` 里的**客户端兜底**分支。真实后端 auth 错误（`detail` 非空时）在 Phase 4 后端加 error-code/Accept-Language 之前**仍显示中文**。DoD 必须显式声明此缺口、知会项目主。

---

## 执行步骤

> 约定：独立 worktree + 分支 `uiloc/min-auth-en`；每步用**显式 pathspec** commit（`git commit -- <files>`），**禁 `git add .`**（会误纳 `.codegraph/` / `.codex_worktrees/`）。命令默认 Git Bash / CI Linux，已注明 PowerShell 等价。
> ⚠️ 本仓库 `.git` 是 shallow + 多 worktree 共享：**本地 shell 永不用 `git fetch --depth=1`**（会把 HEAD graft 进共享 `.git/shallow`，push 发不完整 pack 被 reject）。只有 CI 的 base-ref diff 用 `--depth=1`（隔离环境，安全）。

### Step 0 · 确认现状（re-verify file:line —— 多 agent 仓库行号会漂移）

- **动作**：实施前重新核对每个 in-scope 文件的实际内容与行号（本仓库多 agent 并行，下方引用的 file:line 仅为撰写时快照，**不可照抄**）。
- **路径变量（避免手滑，CodeX 二审 #4）**：UI-02 后 `(auth)` 已在 `[locale]/` 下。先定义变量并在后续命令里替换 `app/(auth)`：
  ```bash
  AUTH="frontend-next/src/app/[locale]/(auth)"   # 下文凡 app/(auth) 一律读作 $AUTH
  ```
  PowerShell：`$AUTH="frontend-next/src/app/[locale]/(auth)"`。下文验收命令里出现的 `frontend-next/src/app/(auth)/...` 均以 `$AUTH/...` 执行。
- **涉及文件**（全部 in-scope）：
  - `frontend-next/src/app/(auth)/auth/page.tsx`（撰写时 54 行，注册页主入口）
  - `frontend-next/src/app/(auth)/auth/register/page.tsx`（撰写时 53 行，与上者**近重复**：仅 JSDoc 注释 + 首页 `<Link>` 的 `aria-label` 不同，正文 DOM 一致）
  - `frontend-next/src/app/(auth)/auth/login/page.tsx`
  - `frontend-next/src/app/(auth)/auth/forgot-password/page.tsx`
  - `frontend-next/src/components/auth/phone-login-form.tsx`
  - `frontend-next/src/components/auth/email-register-form.tsx`
  - `frontend-next/src/components/auth/register-method-form.tsx`
  - `frontend-next/src/components/auth/password-login-form.tsx`（含 `LOGIN_ERROR_MESSAGES` map，撰写时 13-16 行）
  - `frontend-next/src/components/auth/captcha-gate.tsx`（GeeTest `language: "zho"` 撰写时 ~212 行；Turnstile `language: "zh-cn"` 撰写时 ~421 行；多处内联中文状态串）
  - `frontend-next/src/lib/api/client.ts`（`statusFallbackMessage` 撰写时 183-192；timeout 串 86-89；`stringifyErrorDetail` 158-181）
  - `frontend-next/src/lib/api/errors.ts`（`getErrorMessage` 默认串撰写时第 6 行）
- **确认 UI-01/UI-02 已落地**：`messages/zh/auth.json` 与 `messages/en/auth.json` 存在（哪怕占位 `{}`）；`src/i18n/navigation.ts` 导出本地化 `Link`/`useRouter`；`useLocale`/`getTranslations`/`useTranslations` 可从 `next-intl` 正常 import。
- **该步验收**（命令在 repo 根执行）：
  ```bash
  test -f "frontend-next/messages/zh/auth.json" && test -f "frontend-next/messages/en/auth.json" && echo OK_CATALOG
  test -f "frontend-next/src/i18n/navigation.ts" && echo OK_NAV
  # 确认两页确为近重复（diff 仅注释/aria-label 行）：
  diff "frontend-next/src/app/(auth)/auth/page.tsx" "frontend-next/src/app/(auth)/auth/register/page.tsx" || true
  ```
  PowerShell 等价：`Test-Path frontend-next/messages/zh/auth.json`；`Compare-Object (Get-Content '...auth/page.tsx') (Get-Content '...auth/register/page.tsx')`。
- **若 UI-01/UI-02 未就绪**：停。本单元 hard-block 于它们，不得自建 catalog/routing 绕过依赖。

### Step 1 · dedupe 两个近重复注册页（翻译前先去重 — Task 1.5.1）

- **动作**：把 `auth/page.tsx` 与 `auth/register/page.tsx` 的共享正文抽成一个 client 组件，两页各自只做薄包裹。**先 dedupe，后抽串**（避免把同一段中文抄进字典两次、漂移）。
- **涉及文件**：
  - 新建 `frontend-next/src/components/auth/register-page-content.tsx`（client 组件，承载现共享 DOM：brand mark + 顶部「登录」链接 + 标题区 + `RegisterMethodForm` + 底部「已有账号？返回登录」）。
  - 改 `frontend-next/src/app/(auth)/auth/page.tsx` → 渲染 `<RegisterPageContent />`。
  - 改 `frontend-next/src/app/(auth)/auth/register/page.tsx` → 同样渲染 `<RegisterPageContent />`。
- **具体改法**：
  - 共享组件保留现有 `<Suspense fallback={null}>` 包裹 `RegisterMethodForm`（两页现都有）。
  - 两页的唯一历史差异（首页 `<Link>` 的 `aria-label`、JSDoc 注释）统一为同一份——zh 输出取**现 `/auth` 主入口**那份（`aria-label="AITrans.Video 首页"`）作为 canonical，使 `/auth` 字节不变；`/auth/register` 渲染输出随之与 `/auth` 对齐（这是允许的：红线 1 要求的是"每个 URL 的默认 zh 输出 vs 改造前该 URL 自身"逐字节一致——`/auth/register` 改造前正文本就与 `/auth` 同形，对齐后仍一致）。
  - 内部 `<Link href>` 改用 `@/i18n/navigation` 的 `Link`（UI-02 提供），否则导航丢 `/en` 前缀。
- **该步验收**：
  ```bash
  test -f "frontend-next/src/components/auth/register-page-content.tsx" && echo OK_DEDUP
  # 两个 page 文件应都引用共享组件、各自 <40 行：
  grep -l "RegisterPageContent" "frontend-next/src/app/(auth)/auth/page.tsx" "frontend-next/src/app/(auth)/auth/register/page.tsx"
  cd frontend-next && npm run lint && npx tsc --noEmit
  ```
  PowerShell：`Select-String -Path '...page.tsx' -Pattern 'RegisterPageContent'`。

### Step 2 · 抽注册/登录页壳 + 三表单串到 `auth.json`（Task 1.5.2）

- **动作**：把 `register-page-content.tsx`、`login/page.tsx`、`register-method-form.tsx`、`phone-login-form.tsx`、`email-register-form.tsx`、`password-login-form.tsx` 的面向用户字符串迁入 `messages/{zh,en}/auth.json`，client 组件用 `useTranslations('auth')`。
- **涉及文件**：上述 6 个组件 + `frontend-next/messages/zh/auth.json` + `frontend-next/messages/en/auth.json`。
- **具体改法**：
  - **namespace 切片**（§1.4 性能红线）：auth 串只进 `auth` namespace；`NextIntlClientProvider` 用 `pick` 只给 auth 子树下发到 client（provider 配置在 UI-02 的 `[locale]/layout`，本单元只新增 key，不改 provider 下发策略；若 auth 子树未被 `pick`，在本单元补上 `auth` 到该 route group 的下发列表）。
  - **ICU 串**（禁拼接）：
    - phone-login-form 倒计时：`{remaining}s 后可重发`（zh）→ ICU `"resendCountdown": "{remaining}s 后可重发"` / en `"{remaining}s to resend"`。
    - phone-login-form 手机号回显：`已向 {phone} 发送验证码` → ICU，`{phone}` 是 content 透传（红线 4）。
    - 密码位数 `至少 12 位` / `密码至少 12 位`：抽成 ICU `{min} 位`（现硬编码 12，可保留为 ICU 参数或常量内插）。
  - **状态机文案**（按钮多态）逐条抽：`发送验证码` / `发送中…` / `验证中…` / `验证并继续` / `完成注册` / `注册中…` / `重发验证码` / `使用其他手机号` / `修改邮箱或重新发送` / `验证邮箱` 等。
  - **register/login 页壳**：标题（`注册 AITrans.Video` / `登录 AITrans.Video`）、副标（`默认使用手机号注册…` / `使用手机号或邮箱和密码登录` 两态）、tab（`密码登录`/`验证码登录`、`手机号`/`邮箱`）、`已有账号？返回登录` / `还没有账号？免费注册`、`开始本地化` / `欢迎回来` eyebrow。
  - **客户端校验 toast**（这些是**纯客户端**串，非 server detail，本步直接翻）：`请输入正确的手机号` / `请输入正确的邮箱地址` / `请填写账号和密码` / `请输入验证码` / `请输入邮箱验证码` / `密码至少 12 位` / `两次密码输入不一致` / `两次密码不一致` / `注册令牌无效，请重新开始` / `邮箱验证已失效，请重新验证` 等。
  - **toast.success**（客户端固定文案）：`验证码已发送` / `验证码已发送到邮箱` / `登录成功` / `注册成功，欢迎使用` / `邮箱注册成功，欢迎使用` / `已重新发送` / `邮箱验证通过，请设置登录密码` 等——全部进字典。
  - zh JSON 的值**逐字节照搬现有字面量**（含半/全角逗号差异，红线 1）。en 值为对应翻译（账单/法务无关，可常规翻译，但术语与营销层一致）。
- **该步验收**：
  ```bash
  cd frontend-next
  node -e "const z=require('./messages/zh/auth.json'),e=require('./messages/en/auth.json');const fz=(o,p='')=>Object.entries(o).flatMap(([k,v])=>typeof v==='object'?fz(v,p+k+'.'):[p+k]);const kz=fz(z).sort(),ke=fz(e).sort();if(JSON.stringify(kz)!==JSON.stringify(ke)){console.error('KEY MISMATCH zh vs en');process.exit(1)}console.log('OK_KEYS_PARITY',kz.length)"
  npm run lint && npx tsc --noEmit
  ```
  （Node 脚本：断言 `zh/auth.json` 与 `en/auth.json` key 集合完全一致——漏译一个 key 即红。PowerShell 同样可 `node -e`，跨 shell 一致。）

### Step 3 · forgot-password 页迁移（Task 1.5.2 续）

- **动作**：把 `forgot-password/page.tsx` 的全部面向用户串迁入 `auth.json`，ICU 化模板串。
- **涉及文件**：`frontend-next/src/app/(auth)/auth/forgot-password/page.tsx` + `messages/{zh,en}/auth.json`。
- **具体改法**：
  - 标题/副标：`找回密码` / `通过手机号或邮箱验证码重置登录密码`；tab `手机号`/`邮箱`；label `手机号`/`邮箱`/`验证码`/`新密码`/`确认新密码`；placeholder（`请输入注册时使用的手机号` 等）。
  - **ICU 回显**：`验证码已发送至 {identity}`（`{identity}` 是 phone/email content 透传）。
  - 按钮多态：`发送中...` / `发送验证码` / `重置中...` / `重置密码` / `重新发送验证码`。
  - 客户端校验/兜底 toast：`请输入验证码` / `密码至少 12 位` / `两次密码不一致` / `短信验证码已发送` / `邮箱验证码已发送` / `密码重置成功，已自动登录` / 底部说明 `手机号和已验证邮箱均可用于找回密码。`。
  - 底部 `返回登录` `<Link>` 改用 `@/i18n/navigation`。
  - **注意**：`data?.detail || "验证码发送失败"` 与 `data?.detail || "重置失败"` 里的兜底属 Step 5（客户端兜底集中处理），本步只翻**纯客户端**串与模板；这两个 `|| "<中文>"` 兜底在 Step 5 统一替换。
- **该步验收**：
  ```bash
  cd frontend-next && npm run lint && npx tsc --noEmit
  # 复跑 Step 2 的 key parity 脚本，确保 forgot 新增 key 仍 zh/en 对齐
  ```

### Step 4 · captcha-gate locale 驱动（Task 1.5.3）

- **动作**：把 `captcha-gate.tsx` 里 provider 的语言参数与内联中文状态串改成 active locale 驱动，使英文表单不嵌中文验证码。
- **涉及文件**：`frontend-next/src/components/auth/captcha-gate.tsx`（+ `auth.json` 增 captcha 子键）。
- **具体改法**：
  - 引 `useLocale()`（next-intl），映射到 provider 语言码：
    - **GeeTest**：`language: locale === 'en' ? 'eng' : 'zho'`（GeeTest v4 用 `'eng'`/`'zho'`；以撰写时 `language: "zho"` 那处为锚，re-verify 行号）。
    - **Turnstile**：`language: locale === 'en' ? 'en' : 'zh-cn'`（Cloudflare Turnstile 用 `'en'`/`'zh-cn'`）。
  - 内联中文状态串改 `useTranslations('auth')`：`已完成人机验证` / `安全验证已就绪` / `正在加载人机验证` / `点击验证` / `验证中...` / `加载中...` / `重试` / `点击完成人机验证`（fake gate）/ 各 reject 文案（`人机验证仍在加载…` / `人机验证超时…` / `请先完成人机验证` 等）/ 配置缺失串（`验证码配置缺失（…未设置）`——`NEXT_PUBLIC_*` 变量名是 content，模板 ICU 化保变量名透传）。
  - **不动**：token 解析、`pendingRef`/timeout、provider dispatch、widget render/destroy 逻辑（OUT-OF-SCOPE 安全/控制流）。
  - **`<html lang>` 不另立真源**（红线 5）：locale 只从 `useLocale()` 读，不写。
- **该步验收**：
  ```bash
  cd frontend-next
  # 不应再残留硬编码的 provider 语言字面量（已改为 locale 三元）：
  grep -nE "language:\s*\"(zho|zh-cn)\"" src/components/auth/captcha-gate.tsx && { echo "FAIL: hardcoded captcha language remains"; exit 1; } || echo OK_CAPTCHA_LOCALE
  grep -n "useLocale" src/components/auth/captcha-gate.tsx && echo OK_USELOCALE
  npm run lint && npx tsc --noEmit
  ```
  PowerShell：`Select-String -Path src/components/auth/captcha-gate.tsx -Pattern 'language:\s*"(zho|zh-cn)"'`（命中即 FAIL）。

### Step 5 · 客户端兜底串集中本地化（仅 auth 流命中处 — Task 1.5.4）

- **动作**：把 auth 流会命中的 `data?.detail || "<中文>"` 客户端兜底、`password-login-form` 的 `LOGIN_ERROR_MESSAGES`、以及 `client.ts`/`errors.ts` 里 auth 请求会经过的中文兜底，参数化为可本地化串。**server `detail` 仍原样透传（中文）——本步只换兜底分支**。
- **涉及文件**：
  - 各表单的 `|| "验证码发送失败"` / `|| "验证失败"` / `|| "注册失败"` / `|| "邮箱验证失败"` / `|| "重置失败"` / `|| "登录失败"` 兜底（phone-login-form / email-register-form / password-login-form / forgot-password）。
  - `password-login-form.tsx` 的 `LOGIN_ERROR_MESSAGES`（现 `csrf_origin_rejected` → 中文）：泛化为「server error-code → 本地化串」的小 map（key=后端 code，value=`t('auth.errors.<code>')`）。**注意红线 7**：此 map 只覆盖**已知 code**；后端多数 auth 错误今天**无 code、是裸中文 prose**，仍走 `detail` 原样显示（中文）。本步只把已编码的 `csrf_origin_rejected` 一类纳入字典，不强行 mask 未编码错误。
  - `frontend-next/src/lib/api/client.ts`：`statusFallbackMessage` 的中文（`登录已过期，请重新登录` 等）、timeout 串（`请求超时（{n} 秒无响应）…`）。**本单元只把它们改成从字典取**（ICU `{seconds}` 秒），**不**重排错误层架构（架构重排=Phase 3 / UI-09）。⚠️ `client.ts` 是 server+client 共用、非 React 组件 → 不能用 `useTranslations` hook；改法用 next-intl 的**非 hook** 取数（`getTranslations` server 侧 / 传入 locale 的纯函数 message getter），或保留中文兜底常量但加 `// TODO(UI-09): 错误层集中本地化` 标记 + 在 DoD 声明此处仍中文。**优先**：若取数复杂度高，本步只迁表单层兜底，`client.ts`/`errors.ts` 留给 UI-09，并在 DoD 明确缩限——不要为此引入 hook-in-non-component 反模式。
  - `frontend-next/src/lib/api/errors.ts`：`getErrorMessage` 默认串 `请求失败，请稍后重试。` 同上处理。
- **具体改法**：
  - 表单兜底：`toast.error(data?.detail || t('auth.errors.sendCodeFailed'))` 等——`detail` 优先（仍中文，已知缺口），兜底走字典。
  - **绝不**改变 `fetch` 行为、不吞错误、不改 `res.ok` 判定（红线 2）。
- **该步验收**：
  ```bash
  cd frontend-next
  # auth 表单内不应再有裸中文兜底字面量（detail 分支保留；兜底走 t(...)）：
  grep -rnE "\|\|\s*\"[^\"]*[一-龥]" src/components/auth/ src/app/\(auth\)/ && { echo "FAIL: raw zh fallback remains in auth"; exit 1; } || echo OK_NO_RAW_FALLBACK
  npm run lint && npx tsc --noEmit && npm run build
  ```
  （`npm run build` 在最后一步跑一次完整 standalone 构建，确保整链无 RSC/hydration/import 错误。PowerShell：`Select-String -Path src/components/auth/* -Pattern '\|\|\s*"[^"]*\p{IsCJKUnifiedIdeographs}'`。）

### Step 5.6 · 登录 / 登出后的 locale 连续性（PR #48 @codex 评审吸纳，2026-06-25）

- **来源**：UI-02（PR #48）@codex bot 报了 2×P2，经评估属**认证流的 locale 连续性**——UI-02 Step 3 显式把 `window.location.href` / 登出 / 后端 `/auth/logout` 留原生，post-auth 回跳默认目标也非本单元射程，故**转入本单元**（P1.5「防漏斗断」正是为此）。
- **动作**：让登录成功、登出两条回跳路径保留访客当前 locale，使 `/en` 漏斗在登录前后不掉回中文。
- **涉及文件**：
  - `frontend-next/src/lib/auth/post-auth-redirect.ts`（登录成功默认目标硬编码 `/translations/new`——无 `from` 的直接登录会丢 locale；受保护页跳登录已由 UI-02 proxy 注入带 locale 前缀的 `from`，那条路径已 OK，只缺**默认目标**这一支）。
  - `frontend-next/src/components/app-shell.tsx`（登出 `window.location.href = "/auth/login"` 硬跳——`/en/workspace` 登出应到 `/en/auth/login`）。
  - 相关 auth 表单调用 `post-auth-redirect` 的入口（确认默认目标改造后仍按 `from` 优先、默认随 locale）。
- **具体改法**：
  - 默认 post-auth 目标改为 locale-aware：用 next-intl navigation 的 `useRouter`（client）按当前 locale 产出 `/translations/new`（zh 裸 / en 带 `/en`），或在 `post-auth-redirect` 取数处传入当前 locale 由其拼前缀。**`from` 仍最高优先**（它已含 locale 前缀，verbatim 用）。
  - 登出回跳：把 `window.location.href = "/auth/login"` 改为带当前 locale 前缀（`/en/auth/login` / `/auth/login`）。若必须保留硬导航（清 session 需整页刷新），则用 `useLocale()` 读当前 locale 拼前缀，**不**走 i18n router（保持整页跳的语义），只补 locale 前缀字符串。
  - **红线**：不改 `fetch`/登出 endpoint/method（红线 2）；locale 只**读** `useLocale()`，不另立 lang 真源（红线 5）。
- **该步验收**：
  ```bash
  cd frontend-next
  # 登出硬跳不再裸 /auth/login（应带 locale 前缀逻辑）：
  grep -n "window.location.href" src/components/app-shell.tsx
  # 默认 post-auth 目标不再硬编码裸 /translations/new（应 locale 驱动）：
  grep -n "translations/new" src/lib/auth/post-auth-redirect.ts
  npm run lint && npx tsc --noEmit
  ```
  **运行态手测**：en 营销页直接点「登录」→ 登录成功落 `/en/translations/new`（非 `/translations/new`）；`/en/workspace` 登出 → `/en/auth/login`。

### Step 6 · 收口验收 + 缺口声明

- **动作**：跑全套验收命令 + 在 PR 描述与本单元 DoD 显式声明「server detail 仍中文」缺口、知会项目主（主方案 Task 1.5.4 末句要求）。
- **涉及文件**：无新增改动；汇总验收。
- **该步验收**（汇总，全绿才算完）：
  ```bash
  cd frontend-next && npm run lint && npx tsc --noEmit && npm run build
  ```
  + Step 2 的 key-parity 脚本绿 + Step 4 的 captcha grep 绿 + Step 5 的 no-raw-fallback grep 绿 + CJK 守卫（§5）对本单元改动文件不报新增内联（迁移后 auth 文件的可译串已进字典，应从 baseline 移除而非新增）。

---

## 测试计划

> 现状：`frontend-next` **无 JS 单元测试运行器**（`package.json` scripts 仅 `dev`/`build`/`start`/`lint`；CI `frontend` job = `npm run lint` + `npx tsc --noEmit`）。因此本单元用 **build + lint + tsc + grep/node 断言脚本** 作机器可验收手段，不引入 vitest/jest（引测试栈超出本单元射程，留给质量 TU 轨）。

### 新增

1. **zh/en key parity**：`messages/zh/auth.json` 与 `en/auth.json` key 集合完全一致——漏译即非零退出。**复用 UI-01 的 `npm run uiloc:key-parity`**（它已遍历全 namespace，auth 自动纳入），无需自造检查脚本。
2. **captcha locale grep（Step 4）**：`captcha-gate.tsx` 不再含硬编码 `language: "zho"|"zh-cn"`，且 import 了 `useLocale`。
3. **no-raw-fallback grep（Step 5）**：`src/components/auth/` 与 `app/(auth)/` 内无 `|| "<中文>"` 裸兜底（`detail` 分支除外，detail 是变量非字面量，不被该正则命中）。
4. **build 绿**：`npm run build` 通过（标准 next standalone 构建，覆盖 RSC/hydration/import 完整性）。

### 回归（必含「默认 zh 字节一致」）

5. **默认 zh 字节一致（§5 守卫 2，关键回归）**：对 `/auth`、`/auth/login`、`/auth/register`、`/auth/forgot-password` 四页的默认中文渲染做 DOM 文本快照，断言改造后与改造前**逐字节一致**。落地方式（按 UI-01 给出的 zh-snapshot 基线机制，本单元复用，不自造）：
   - 若 UI-01 已建 zh-snapshot 测试，把这 4 页**加入**其覆盖清单（append）。
   - 若尚无快照工具，最小手段：`npm run build` 后对 4 页 SSR 输出做 `git diff` 级人审 + 截图比对（320/768/1024/1440 见 Phase 1 布局轨，但本单元 zh 不变，重点是英文长文案不溢出）。
   - **dedupe 特例**：`/auth/register` 渲染输出对齐到 `/auth` canonical（aria-label 统一）——回归断言对象是「该 URL 改造后 vs 改造前**该 URL 自身**」，二者改造前已同形，故仍一致。
6. **captcha 安全/控制流不破**：人审确认 Step 4 未改 token 解析 / pending / timeout / provider dispatch（OUT-OF-SCOPE 逻辑零改动）。
7. **网络契约不破**（红线 2）：人审/grep 确认所有 `fetch` 的 endpoint / method / payload 字段未变（只换展示串）。
8. **CJK 守卫 baseline 只减不增**（§5 守卫 1）：本单元迁移完成的 auth 串从 CJK baseline **移除**；不得新增未登记内联 CJK。

---

## 回滚方案

- **提交边界**：本单元在分支 `uiloc/min-auth-en` 上分步提交（Step 1 dedupe / Step 2-3 翻译 / Step 4 captcha / Step 5 兜底），squash-merge 为单一 commit 落 main（便于整体 revert）。
- **优先 `git revert`**：因纯表现层、无 schema/无后端、无路由结构改动，单个 squash commit `git revert <sha>` 即可干净回退到「auth 全 zh、catalog 在但 auth 子树未接」状态。
- **涉及回滚文件**：`src/components/auth/register-page-content.tsx`（新增，revert 即删）、`app/(auth)/auth/{page,register/page,login/page,forgot-password/page}.tsx`、`src/components/auth/{phone-login-form,email-register-form,register-method-form,password-login-form,captcha-gate}.tsx`、`messages/{zh,en}/auth.json`、`src/lib/api/{client,errors}.ts`（仅 Step 5 若改动）。
- **紧急止血（不整体回退）**：把 `messages/en/auth.json` 的值临时填回中文（或令 `routing.ts` 收 `locales` 到 `['zh']`，由 UI-02 控制）——en URL 退回 zh，zh 不受影响。captcha locale 三元若出问题，临时改回 `'zho'`/`'zh-cn'` 常量即恢复中文验证码。

---

## 完成定义（DoD）

- [ ] `auth/page.tsx` 与 `auth/register/page.tsx` 已 dedupe 为共享 `register-page-content.tsx`，两页各 <40 行且引用共享组件（grep 可验）。
- [ ] `auth/login`、register（phone + email + method 三表单）、`forgot-password` 的面向用户串全部进 `messages/{zh,en}/auth.json`；zh 值逐字节照搬原文（含半/全角逗号）。
- [ ] `messages/zh/auth.json` 与 `en/auth.json` key 集合完全一致（key-parity node 脚本绿）。
- [ ] 倒计时 / 手机号回显 / 秒数·位数 / 超时秒数等模板串全部 ICU 化，无字符串拼接（grep 无 `` `${...}秒/位/s 后` `` 残留）。
- [ ] `captcha-gate.tsx` 的 GeeTest/Turnstile `language` 参数与内联状态串 locale 驱动（grep 无硬编码 `"zho"`/`"zh-cn"`，含 `useLocale`）；token/timeout/dispatch 逻辑零改动（人审）。
- [ ] auth 流客户端兜底串（各表单 `|| "<中文>"`、`LOGIN_ERROR_MESSAGES`）已本地化；`detail` 分支保留原样透传。
- [ ] **默认 zh 字节一致**回归通过：`/auth` `/auth/login` `/auth/register` `/auth/forgot-password` 四页默认中文渲染与改造前逐字节相同（snapshot 或人审+构建比对）。
- [ ] `npm run lint` + `npx tsc --noEmit` + `npm run build` 全绿。
- [ ] 所有 `fetch` endpoint/method/payload 未变（红线 2，grep/人审）；未触碰任何 pipeline 语言字段或付费 API 路径（红线 3）。
- [ ] CJK 守卫 baseline 对已迁移 auth 串**只减**、无新增未登记内联 CJK。
- [ ] **已知缺口显式声明（红线 7 / §1.9 / R4）**：PR 描述 + 本 DoD 注明「服务端 `detail`（登录失败 / 验证码错误 / 限频等真实后端 auth 错误）在 Phase 4 后端加 error-code/Accept-Language 之前**仍显示中文**；本单元只 mask 客户端兜底」，并知会项目主。
- [ ] `client.ts`/`errors.ts` 若未在本单元迁移（因 hook-in-non-component 取数复杂度），在 DoD 明确缩限并留 `// TODO(UI-09)` 标记，转 Phase 3 / UI-09。

---

## 关联

- **主方案**：`docs/plans/2026-06-25-ui-page-locale-switch-plan.md` — Phase 1.5（Task 1.5.1–1.5.4）、§0.3 红线、§1.2a `localeDetection:false`、§1.4 ICU+namespace、§1.5 `<html lang>` 单源、§1.9 server-emitted 边界、§3 阶段表、§7 DoD、§8 风险 R4。
- **前置依赖单元**：
  - **UI-02**（routing：`[locale]` 段 + `proxy` 合并 + 本地化 `navigation`/provider 下发）。
  - **UI-01**（catalog：`messages/{zh,en}/*.json` 骨架 + `i18n/request.ts` 具体 import + `IntlMessages` 类型 + zh-snapshot 守卫机制）。
- **下游/相邻单元**：Phase 2（UI-05+，工作台中央字典）、Phase 3（UI-08 共享 UI primitives / UI-09 客户端错误层集中本地化 + error-code map 推广）、Phase 4（独立后端轨：gateway error-code envelope + Accept-Language）。
- **协调**（§0.5 / §0.6）：本单元全在 `frontend-next/` TS/React，与 Python 质量 TU 单元几乎零重叠；CJK-lint / key-parity 检查若入 CI / pre-commit 须 **append 到 TU-03 既有脚手架**（不覆盖），复用「只阻断新增 / 读 base-ref 基线」模式。
