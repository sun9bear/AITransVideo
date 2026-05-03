# 代码审核报告（复核修订版）

> **日期**: 2026-05-02  
> **复核基线**: 当前工作区 + `HEAD=3539735`（K2 jianying tracking fields；K1 dispatcher 回滚已落地）  
> **范围**: Gateway / Python 后端 / frontend-next / tests / 输出链路关键模块  
> **说明**: 本版基于初版审核报告、后续代码核对、Claude Code 复核意见重新整理。已删除或降级与当前代码不符、严重性偏高、修复建议不准确的项。

---

## 总览

| 维度 | CRITICAL | HIGH | MEDIUM | LOW |
|------|----------|------|--------|-----|
| 安全 | 0 | 1 | 4 | 3 |
| 架构合规 | 0 | 0 | 0 | 2 |
| 关键路径正确性 | 0 | 0 | 4 | 1 |
| 前端质量 | 0 | 2 | 3 | 1 |

**当前有效问题总计**: 0 CRITICAL / 3 HIGH / 11 MEDIUM / 7 LOW  
**已移出当前问题列表**: 1 项（Jianying dispatcher PublishError，当前 HEAD 已回滚相关接入）

---

## 复核修订摘要

### 已删除

#### 原 HIGH-3: Jianying PublishError 整条 dispatch 崩溃

- **原结论**: `_build_jianying_request()` 缺 artifact 时抛出 `PublishError`，会让整条 `dispatch()` 失败。
- **复核结论**: 该问题对旧提交 `5dbd1da` 成立，但当前 `HEAD=3424584` 已通过 K1 rollback 删除 `OutputDispatcher` 自动生成 Jianying 草稿的接入。当前 `src/modules/output/output_dispatcher.py` 没有 `_maybe_generate_jianying_draft()` / `_build_jianying_request()`。
- **处理**: 从当前有效问题列表删除。后续如果恢复 J6 dispatcher 接入，需要重新审该风险。

### 已降级 / 改写

| 原编号 | 调整 | 原因 |
|--------|------|------|
| HIGH-1 CSRF | 降为 MEDIUM | `SameSite=strict` + CORS origin 白名单已显著缓解跨站请求风险；仍建议加 Origin/Referer 校验。 |
| MEDIUM-5 catch 过窄 | 保留 MEDIUM，但修正建议 | 不应盲目 `except Exception`，否则会隐藏编程错误。应只包装明确属于 LLM/响应解析/校验失败的异常。 |
| MEDIUM-6 TTS except 过宽 | 保留 MEDIUM，但修正描述 | `KeyboardInterrupt` 不是 `Exception` 子类，原例子错误；真实风险是 `MemoryError` 等运行时资源问题被当作普通段失败重试。 |
| MEDIUM-9 middleware matcher | 降为 LOW | 函数体已 early return 公共路径，主要是额外执行 middleware，不是功能性阻断。 |
| MEDIUM-10 label 缺 htmlFor | 保留 MEDIUM，但拆分 | `746/753` 是真问题；`986` 是合法嵌套 label-input，不算误报。 |

---

## 一、安全

### HIGH-1: Admin 删除/取消任务缺少路径白名单

- **文件**: `gateway/admin_settings.py:921,976`
- **描述**: `cancel_job` 和 `delete_job` 直接调用 `shutil.rmtree(project_dir, ignore_errors=True)`，未校验 `project_dir` 是否位于允许删除的项目根目录下。
- **证据**: `gateway/project_cleanup.py:66-101` 已有 `_is_safe_project_dir()`，但 admin 删除路径未复用。
- **风险**: 若 Job API 返回的 `project_dir` 被污染，admin 操作可能误删非项目目录；`ignore_errors=True` 还会掩盖异常。
- **建议**: 删除前统一调用 `_is_safe_project_dir(Path(project_dir))`；校验失败时拒绝删除并记录告警。

### MEDIUM-1: 缺少 CSRF Origin/Referer 校验

- **文件**: `gateway/auth.py:80-88`, `gateway/main.py:214-219`
- **描述**: Session cookie 设置了 `httponly=True, samesite="strict", secure=True`，CORS 也使用白名单 origin，但 state-changing 端点没有额外的 `Origin`/`Referer` 校验或 CSRF token。
- **风险判断**: 不应评为 HIGH。`SameSite=strict` 对普通跨站请求已有强缓解；剩余风险主要来自子域接管、同站不同源、浏览器/代理异常等边界场景。
- **建议**: 短期增加 state-changing 请求的 `Origin`/`Referer` 白名单校验；长期再考虑 CSRF token。

### MEDIUM-2: 密码登录无暴力破解防护

- **文件**: `gateway/auth.py:182-235`
- **描述**: 密码登录无 per-account / per-IP 限流或锁定机制。手机号验证码发送路径已有 rate limiting，但密码路径没有。
- **建议**: 增加账号维度和 IP 维度限流；失败日志应避免泄露账号是否存在。

### MEDIUM-3: `shadow_reserve` 脏 ORM 状态风险

- **文件**: `gateway/credits_service.py:295-331`
- **描述**: `shadow_reserve` 先执行 `bucket.reserved += take`，异常后只记录日志并返回 `[]`。如果调用方随后继续 commit 同一 session，脏 ORM 状态可能被持久化。
- **建议**: 在 `except` 中显式 rollback / refresh / 重置已修改 bucket；更稳妥是用 savepoint 包住 shadow 操作。

### MEDIUM-4: `ApiClient` 未显式携带 credentials

- **文件**: `frontend-next/src/lib/api/client.ts:49`
- **描述**: `ApiClient.request()` 没有设置 `credentials: "include"`。如果该类用于跨 origin 的认证请求，cookie 不会被发送。当前多数认证请求绕过该类直接 raw fetch，但这是后续复用时的陷阱。
- **建议**: 默认加 `credentials: "include"`，或明确分出 authenticated client / public client。

### LOW-1: Session cookie 未使用 `__Host-` 前缀

- **文件**: `gateway/auth.py:80-88`, `gateway/config.py:42`
- **描述**: Cookie 名称为 `avt_session`，未使用 `__Host-` 前缀。
- **建议**: 可迁移为 `__Host-avt_session`。迁移时需兼容旧 cookie 名，避免用户批量掉线。

### LOW-2: 缺少统一安全响应头

- **文件**: `gateway/main.py`
- **描述**: Gateway 未统一设置 `X-Content-Type-Options`、`X-Frame-Options`、基础 CSP 等安全响应头。
- **建议**: 增加中间件设置标准安全头。CSP 需结合前端资源加载策略单独验证。

### LOW-3: 依赖版本约束偏宽

- **文件**: `pyproject.toml:7-14`
- **描述**: 多个运行时依赖没有上限版本或 lockfile 约束，未来安装可能引入破坏性变更。
- **建议**: 生产部署使用 lockfile；关键依赖加兼容范围约束。

---

## 二、架构合规

核心不变性复核结论：当前未发现违反项目架构不变性的代码。

- TTS 单位仍以 `SemanticBlock` 为核心。
- 对齐仍是 DSP-first，rewrite 是后备。
- 字幕 retiming 仍走确定性逻辑。
- 主交付目标仍围绕剪映草稿 / 编辑产物，而不是直接 MP4。
- 商业事实仍主要由 Gateway 提供。
- 测试与默认路径仍以 mock/stub/fake 为主。

### LOW-4: Admin 表单默认值硬编码

- **文件**: `frontend-next/src/app/(app)/admin/settings/page.tsx:39`, `frontend-next/src/app/(app)/admin/users/page.tsx:53`
- **描述**: admin 表单里存在 `free_user_max_duration_minutes: 10`、`free_jobs_quota_total: 5` 等默认值。它们不是终端用户 pricing source of truth，但可能随 Gateway catalog 漂移。
- **建议**: 从 Gateway API 获取默认值；至少在 UI 里把这些值作为加载失败 fallback。

### LOW-5: `RewriteEngine` 固定绑定 `MockLLMService`

- **文件**: `main.py:271`
- **描述**: `_build_project_workflow()` 中 `RewriteEngine(llm_service=MockLLMService())` 固定使用 mock。当前阶段可接受，但真实 rewrite 能力没有运行时注入路径。
- **建议**: 标记为已知限制；如要启用真实 rewrite，应走显式配置开关并保持默认 mock。

---

## 三、关键路径正确性

### MEDIUM-5: 翻译 checkpoint 非原子写入

- **文件**: `src/services/gemini/translator.py:513,2469-2474`
- **描述**: `_write_json()` 使用 `Path.write_text()` 直接写入。进程崩溃或磁盘异常时可能留下半写 JSON；恢复时若解析失败，会丢失已有翻译进度。
- **建议**: 复用 `src/utils/atomic_io.py` 的原子写入能力，或按 temp + fsync + rename 实现。

### MEDIUM-6: LLM fallback catch 范围偏窄，但不应盲目扩大

- **文件**: `src/services/gemini/translator.py:1181,1264`
- **描述**: `_call_task_with_fallback` 只捕获 `(TranslationError, LLMProviderError)`。如果 callee 链上的 LLM 调用、响应解析或格式校验抛出未包装的可预期异常，可能不会触发 fallback。
- **修正说明**: 初版建议加 `except Exception` 不合适，会把 `AttributeError` 等编程错误也转成 fallback，掩盖真实 bug。
- **建议**: 将 LLM 调用、响应解析和 validator 的可预期失败包装成 `TranslationError`，或只捕获明确的响应解析/传输异常，如 `ValueError` / `json.JSONDecodeError` / provider HTTP error，并保留编程错误直接失败。

### MEDIUM-7: TTS 并行失败处理会把 `MemoryError` 当普通段失败重试

- **文件**: `src/services/tts/tts_generator.py:367-373,391-395`
- **描述**: 并行生成路径 `except Exception` 会捕获 `MemoryError` 等资源异常，并把段加入重试队列；这类异常通常不应 5 分钟后重试。
- **修正说明**: `KeyboardInterrupt` / `SystemExit` 不属于 `Exception`，初版报告举例错误；`(TTSGenerationError, Exception)` 与 `except Exception` 等价，也不是有效修复。
- **建议**: 显式识别 `MemoryError` 等资源异常并立即失败；普通 provider / network / TTS 输入异常再进入段级重试。

### MEDIUM-8: `shadow_capture` 允许透支但缺少告警边界

- **文件**: `gateway/credits_service.py:560-572`
- **描述**: actual 超过 reserved 且 bucket 余额不足时，`capture_overdraft` 会让 `bucket.remaining` 变负。这符合 shadow accounting 的“保留真实消耗”意图，但缺少强告警和对账标识。
- **建议**: 保留透支记录，但增加 warning/metric，并在后台对账中显式统计 overdraft。

### LOW-6: 套餐升级 rank 字典可能漂移

- **文件**: `gateway/billing.py:107-112`
- **描述**: `create_order` 使用硬编码 `plan_rank = {"free": 0, "plus": 1, "pro": 2}`。如果 Gateway catalog 新增付费套餐，升级校验可能与 runtime catalog 漂移。
- **建议**: 从 plan catalog 派生 rank；或在新增 plan 的迁移 checklist 中包含该处更新。

---

## 四、前端质量

### HIGH-2: 系统化静默吞错

- **范围**: `frontend-next/src/components/providers/session-provider.tsx`、workspace 表单、voice selection、voices/admin voices 页面等。
- **描述**: 多处 `.catch(() => {})` 或 catch 后返回空数组，导致“请求失败”和“数据为空”无法区分。
- **影响**: 用户看到空白或未登录状态，运维也缺少定位信号。
- **建议**: 分层治理：认证/session 先区分 401 与 5xx/网络错误；业务数据加载失败显示 inline error 或 toast；只在明确可忽略的 telemetry 路径静默。

### HIGH-3: 全站缺少 `error.tsx` / `loading.tsx`

- **范围**: `frontend-next/src/app`
- **描述**: 当前 route segment 下没有 `error.tsx` / `loading.tsx`。Server component 或 async 页面异常时缺少恢复 UI；加载时也容易出现空白。
- **建议**: 至少在 `app/`、`app/(marketing)/`、`app/(app)/` 增加基础 error boundary 和 loading skeleton。

### MEDIUM-9: 登录模式切换缺少 tab / pressed ARIA

- **文件**: `frontend-next/src/app/(auth)/auth/login/page.tsx:37-62`
- **描述**: “密码登录 / 验证码登录”是互斥模式切换，但按钮没有 `role="tab"`、`aria-selected` 或 `aria-pressed`。
- **建议**: 按 tablist/tab 模式实现，或使用 pressed button 模式；保持键盘可用性。

### MEDIUM-10: `SessionProvider` 无 error 状态和重试

- **文件**: `frontend-next/src/components/providers/session-provider.tsx:34-43`
- **描述**: `/auth/me` 失败后直接吞错并 `loading=false`，网络/500 与真实 401 都会表现为未登录。
- **建议**: 区分 401 与网络/5xx；非 401 时暴露 `error` 和 retry。

### MEDIUM-11: `VoiceSelectionPanel` 假 radio 使用 label

- **文件**: `frontend-next/src/components/workspace/VoiceSelectionPanel.tsx:746,753`
- **描述**: 两处 `<label>` 只包 `<span>` 并通过 `onClick` 切换状态，没有真实 input，也没有 radio 语义。
- **修正说明**: `:986` 的 `<label><input ... /></label>` 是合法嵌套关联，不算问题。
- **建议**: 改成真实 radio input，或使用 `button role="radio"` + `aria-checked`。

### LOW-7: middleware matcher 过宽

- **文件**: `frontend-next/src/middleware.ts:68-72`
- **描述**: matcher 未排除 marketing/legal 等公共页面，但函数体内 `publicExactPaths` / `/marketing/` 已 early-return。
- **影响**: 主要是多一次 middleware 执行，不是功能问题。
- **建议**: 低优先级优化 matcher negative lookahead，减少无意义执行。

---

## Pass 项（修订后）

| 领域 | 结论 |
|------|------|
| SQL 安全 | 业务请求路径主要使用 SQLAlchemy ORM / 参数化查询。迁移脚本和维护脚本存在 raw SQL，但未发现用户输入拼接进入 SQL 的证据。 |
| 输入验证 | `auth_phone.py` Pydantic 约束、phone normalize；`upload.py` 路径净化；`job_intercept.py` display_name 过滤，整体方向正确。 |
| 路径安全模型 | `project_cleanup.py` 的 `_is_safe_project_dir()` 是可复用的安全删除模型，应推广到 admin job 删除。 |
| 数据持久化 | 多个状态文件路径已使用 temp + rename；翻译 checkpoint 是当前需要补齐的例外。 |
| 架构一致性 | 六个核心不变性当前未见违反。 |
| 定价数据 | 终端用户 pricing UI 主要从 Gateway API 获取；admin fallback 默认值仍需治理漂移。 |
| 支付幂等 | Webhook / provider event 幂等设计方向正确。 |
| 音色与速度决策 | `VoiceRegistry` priority chain、`SpeedDecision` 确定性逻辑符合当前阶段原则。 |

---

## 本 sprint 建议优先修

| 优先级 | ID | 描述 | 建议改动 |
|--------|----|------|----------|
| P0 | HIGH-1 | Admin `rmtree(project_dir)` 缺路径白名单 | 复用 `_is_safe_project_dir()`，拒绝不安全路径 |
| P1 | HIGH-2 | 前端系统化吞错 | 先治理 session / voice / workspace 关键请求 |
| P1 | HIGH-3 | 缺少 error/loading boundary | 为 app / marketing / app workspace 增加基础边界 |
| P2 | MEDIUM-2 | 密码登录无暴力破解防护 | 增加 per-account / per-IP 限流 |
| P2 | MEDIUM-5 | 翻译 checkpoint 非原子写入 | 改用 atomic write |
| P2 | MEDIUM-3 | `shadow_reserve` 脏 ORM 状态 | savepoint 或异常 rollback/reset |
| P2 | MEDIUM-1 | CSRF Origin/Referer 校验 | 对 state-changing 端点加同源校验 |
| P3 | MEDIUM-9 | 登录切换 ARIA | 补 tab/pressed 语义 |
| P3 | MEDIUM-11 | VoiceSelectionPanel 假 radio | 改真实 radio 或 `role="radio"` |

---

## 后续注意

- Jianying dispatcher 自动生成接入当前已回滚。后续如按 on-demand 方案重新接入，必须重新审：
  - 缺 artifact 是否只影响 Jianying 草稿，不影响 editor/publish 主产物；
  - 失败报告是否进入 manifest；
  - `pyJianYingDraft` 缺失时是否 graceful skip。
- 本报告不应直接覆盖未提交的 Codex 热修工作流。部署前需先整理当前 working tree，避免用 `origin/main` 回滚线上 hotfix。
