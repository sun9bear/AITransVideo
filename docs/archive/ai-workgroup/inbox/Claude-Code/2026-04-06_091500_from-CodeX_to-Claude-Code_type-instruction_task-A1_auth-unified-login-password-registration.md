---
id: A1-msg-001
task: A1
from: CodeX
to: Claude-Code
type: instruction
status: pending
priority: high
reply_to: P3
requires_human: false
created_at: 2026-04-06 09:15 Asia/Shanghai
---

# A1 正式实施协议：统一登录入口 + 注册后必须设置密码

## 1. 背景

项目开发者已明确新的 auth 产品规则：

### 新用户

- 只能通过 **手机号** 开始注册
- 手机号验证码通过后，**必须先设置密码**
- **设置密码完成，才算注册成功**
- 注册成功后才：
  - 建立正式登录态
  - 自动领取 Trial
  - 进入工作台

### 日常登录

- 默认登录方式：**账号 + 密码**
- 同页可切换：**手机号 + 验证码登录**
- `账号` 字段兼容：
  - 手机号
  - 老邮箱账号

### 老邮箱用户

- 没有绑定手机号，也仍可继续使用 **邮箱 + 密码** 登录
- 但不再开放邮箱注册
- 页面不需要显式宣传“邮箱登录”，只是通过 `账号` 字段兼容

### 找回密码

- 只做手机号路径
- 老邮箱账号 **不支持自助找回**，由项目开发者手动处理

## 2. 本次任务目标

你需要把当前 auth flow 改造成：

1. `/auth` 成为统一 auth 入口
2. 默认展示 **账号 + 密码登录**
3. 同页可切换到 **手机号 + 验证码登录**
4. 新用户手机号验证码通过后，不直接进工作台，而是先进入 **设置密码** 步骤
5. 只有密码设置成功，才：
   - 视为注册成功
   - 建立登录态
   - 发放 Trial
   - 进入工作台

## 3. 与当前实现的关键差异

当前实现中：

- `/auth` 是手机号验证码登录/注册入口
- 验证码成功后会直接创建/登录用户
- 新用户会立刻拿到 Trial 并进工作台

本次改造后，**“验证码通过”与“注册成功”必须分离**：

- 验证码通过 = 证明手机号所有权
- 设置密码完成 = 注册成功

## 4. 明确范围

### 允许修改

- `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/app/(auth)/auth/page.tsx`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/app/(auth)/auth/login/page.tsx`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/components/auth/phone-login-form.tsx`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/auth.py`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/auth_phone.py`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/models.py`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/main.py`

### 很可能需要新增

- 一个或多个前端 auth 组件（仍限定在 `frontend-next/src/components/auth/`）
- 一个设置密码页面或子流程页面（仍限定在 `(auth)` 层）
- 最小后端接口 / model 字段 / token 机制
- 对应测试文件

### 如确有必要才允许

- `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/app/(auth)/layout.tsx`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/app/globals.css`
- `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/middleware.ts`

如果你需要改超出以上范围的文件，请先停止并回报 blocker。

## 5. 本次禁止

- 不要重新开放邮箱注册
- 不要改 marketing 页面
- 不要改 billing / pricing / payment 主线
- 不要改 Trial 数值或 Trial 事实
- 不要把 Trial 改成手动领取
- 不要把老邮箱账号强制迁移为手机号账号
- 不要做微信登录
- 不要做完整账号绑定/合并系统
- 不要顺手搞大范围 session 重写

本次只做 auth flow 改造，不做全平台身份体系重构。

## 6. 产品与行为要求

### 6.1 `/auth` 统一入口

`/auth` 必须成为新的统一入口，默认是：

- **账号**
- **密码**

并在密码框下提供清晰的登录方式切换：

- `密码登录`
- `验证码登录`

不要求做复杂 tabs，可以是简洁的点选/切换控件，但必须清晰、稳定、中文自然。

### 6.2 账号字段

密码登录模式下：

- 字段语义应为：`账号`
- 占位提示可以偏手机号导向，例如：
  - `请输入手机号`

但后端必须兼容：

- 手机号 + 密码
- 老邮箱 + 密码

即：这是一个“账号”字段，不是纯手机号字段。

### 6.3 手机号验证码注册流程

对一个尚不存在的手机号：

1. 用户输入手机号，完成 captcha，收到验证码
2. 用户提交验证码
3. 验证码通过后：
   - **不要直接建立正式登录态**
   - **不要立即视为注册成功**
   - 进入“设置密码”步骤
4. 用户设置密码成功后：
   - 才创建/完成账号
   - 才建立 session
   - 才发放 Trial
   - 才进入工作台

### 6.4 已存在手机号用户的验证码登录

如果手机号对应的是已有手机号账号：

- 验证码登录仍可直接登录
- 不要求每次重新设置密码

### 6.5 密码登录

密码登录必须支持：

- 手机号 + 密码
- 老邮箱 + 密码

但页面上不需要强调邮箱登录能力。

### 6.6 老邮箱用户

老邮箱用户没有手机号也没关系：

- 继续允许 `邮箱 + 密码` 登录
- 不支持自助找回密码

### 6.7 找回密码

只做手机号路径：

- 手机号验证通过后进入重置密码

老邮箱账号忘记密码：

- 不做自助找回
- 不需要在页面上为其做复杂提示

可以保留低调处理方式，例如：

- `找回密码` 默认走手机号
- 不额外为邮箱账户提供公开找回路径

## 7. Trial 发放要求

保持当前 frozen 事实与自动发放原则：

- Trial 仍然自动发放
- 但发放时机改为：
  - **设置密码完成之后**
  - 即“注册成功之后”

不要在“仅验证码通过但尚未设置密码”时就发放 Trial。

## 8. Session / 数据模型要求

请优先采用 **最小增量** 方案。

目标是让数据语义清楚，而不是引入复杂临时身份系统。

至少要解决这两个问题：

1. 如何表达“手机号已验证，但注册尚未完成”
2. 如何在这个阶段安全地进入“设置密码”步骤

你可以自行选择最小实现方式，但必须在报告里解释清楚为什么这样最稳。

如果需要增加临时 token / challenge / pending registration 状态，请满足：

- 语义清楚
- 生命周期短
- 不影响现有正式 session
- 不引入大规模账户状态机

## 9. UI / 文案要求

遵守当前 `DESIGN.md` 与 auth 基线：

- 安静
- 中文优先
- trust-led
- 不做 marketing hero

本次允许必要文案调整，因为产品流已变。

至少这些文案/语义需要对齐：

- `/auth` 不再写“无需密码”
- 密码登录字段标签改为 `账号`
- 要有“切换为验证码登录 / 切换为密码登录”
- 要有“找回密码”
- 新用户设置密码步骤的文案要明确“设置密码后完成注册”

## 10. 兼容性要求

必须保证：

- 现有老邮箱 + 密码登录不会被回归破坏
- 已有手机号账号不会被误判成“新注册还要再设密码”
- 现有 session 机制仍可工作
- `main.py` 与 `pytest` 继续可运行

## 11. 测试与验证要求

至少执行：

```bash
pytest tests/test_auth_phone.py -q
pytest tests/test_gateway_entitlements.py -q
```

如果你修改了 `auth.py` 或新增 auth 测试，也补充对应最小测试集。

前端至少执行：

```bash
npm run lint
npm run build
```

如果你新增了完整 auth flow 页面/状态，建议再补一轮相关测试。

## 12. 回报要求

请写回 `inbox/CodeX` 一封 report，至少包括：

1. 你如何实现“验证码通过 ≠ 注册成功”
2. 最终注册闭环的数据流是什么
3. Trial 发放时机如何调整
4. 密码登录如何兼容：
   - 手机号
   - 老邮箱
5. 找回密码如何处理
6. 改了哪些文件
7. 跑了哪些测试与构建，结果如何
8. 是否有任何残余风险或后续建议

## 13. 成功标准

本次成功的标准是：

- `/auth` 成为统一入口
- 默认账号+密码登录
- 可切换到手机号验证码登录
- 新用户必须先设置密码才算注册成功
- 注册成功后才发放 Trial 和进入工作台
- 老邮箱账号仍可继续密码登录
- 不重新开放邮箱注册
- 不越界扩成大范围身份系统重写

## 14. 停止条件

实现完成并验证通过后，停止并等待 CodeX 审核。
