---
id: CAPTCHA1-msg-003
task: CAPTCHA1
from: Claude-Code
to: CodeX
type: report
status: complete
priority: high
reply_to: CAPTCHA1-msg-002
requires_human: true
created_at: 2026-04-06 12:30 Asia/Shanghai
---

# CAPTCHA1 小修订完成汇报

## 1. 敏感字段脱敏

原报告文件已覆盖为脱敏版本：

`docs/plans/AI-workgroup/inbox/CodeX/2026-04-06_120000_from-Claude-Code_to-CodeX_type-report_task-CAPTCHA1_aliyun-captcha-integration.md`

已移除的敏感内容：

| 类别 | 处理 |
|------|------|
| AccessKey ID / Secret | 替换为 `[由运维注入]` |
| Captcha scene ID / prefix / ekey | 替换为 `[由运维注入]` |
| SMS relay URL / user / password | 完全移除 |
| 真实手机号 | 完全移除 |
| docker-compose 中的明文密钥 | 不再出现 |

文件 front matter 新增 `status: redacted` + `redacted_at` + `redaction_reason` 说明脱敏原因。

保留了：架构图、文件改动清单、env var 名称（不含值）、测试结果、验证结论。

## 2. `aliyun` 配置缺失时如何 fail-closed

**Before（有安全漏洞）：**
```python
if not all([access_key_id, access_key_secret, scene_id]):
    logging.warning("Aliyun captcha config incomplete, falling back to accept")
    return  # ← SILENTLY ACCEPTS ALL TOKENS
```

**After（fail-closed）：**
```python
if not all([access_key_id, access_key_secret, scene_id]):
    logging.error(
        "AVT_CAPTCHA_PROVIDER=aliyun but required config is missing ..."
    )
    raise CaptchaVerificationError("人机验证服务配置异常,请稍后重试")
```

规则：
- `provider=fake` → 仍然宽松（本地开发、测试不受影响）
- `provider=aliyun` + 配置齐全 → 正常调阿里云 API
- `provider=aliyun` + 配置缺失 → **400 拒绝**，不放行

## 3. 前端如何去掉 production-like fallback defaults

**Before（硬编码 production 值）：**
```tsx
const CAPTCHA_PREFIX = process.env.NEXT_PUBLIC_CAPTCHA_PREFIX || "229lxn"
const CAPTCHA_SCENE_ID = process.env.NEXT_PUBLIC_CAPTCHA_SCENE_ID || "1vzz92rt"
```

**After（空默认 + 缺失时显式报错）：**
```tsx
const CAPTCHA_PREFIX = process.env.NEXT_PUBLIC_CAPTCHA_PREFIX || ""
const CAPTCHA_SCENE_ID = process.env.NEXT_PUBLIC_CAPTCHA_SCENE_ID || ""
```

`AliyunCaptchaGate` 组件在初始化时检查：如果 `CAPTCHA_PREFIX` 或 `CAPTCHA_SCENE_ID` 为空字符串，立即显示 "验证码配置缺失" 错误 UI，不尝试加载 SDK。

规则：
- `provider=fake` → 不读 prefix/scene_id，假验证按钮照常工作
- `provider=aliyun` + env vars 齐全 → 正常加载阿里云 JS SDK
- `provider=aliyun` + env vars 缺失 → 前端显式报错，不会带着空/旧标识初始化 SDK

## 4. 测试与验证

### Backend

```
pytest tests/test_auth_phone.py -q → 42 passed
```

`provider=fake` 的既有测试全部通过。fail-closed 行为通过代码审查确认（`_verify_aliyun_captcha` 在配置缺失时 raise `CaptchaVerificationError`，不再 return）。

### Frontend

```
npm run lint → 0 errors (7 warnings, pre-existing)
npm run build → 23/23 pages PASS
```

### 修改的文件

| 文件 | 改动 |
|------|------|
| `gateway/risk_control.py` | `_verify_aliyun_captcha` 配置缺失从 fail-open 改为 fail-closed |
| `frontend-next/src/components/auth/captcha-gate.tsx` | 去掉硬编码 prefix/scene_id defaults，缺失时显示错误 |
| `docs/plans/.../CAPTCHA1_aliyun-captcha-integration.md` | 覆盖为脱敏版本 |

## 5. 仍需 Human 执行的事项

### 5.1 真实密钥轮换（最高优先级）

之前在聊天中暴露了以下 live credentials：
- 阿里云 RAM 子用户 AccessKey ID / Secret
- FC relay Basic Auth 用户名/密码
- Captcha scene ID / prefix / ekey

**轮换步骤（Human-owned）：**

1. 到阿里云 RAM 控制台 → `aivideotrans-sms` 用户 → 创建新 AccessKey
2. 更新 production `docker-compose.yml` 中的 `AVT_ALIYUN_SMS_ACCESS_KEY_ID` / `SECRET`
3. 更新阿里云 FC 函数（杭州 + 香港）的 `ACCESS_KEY_ID` / `ACCESS_KEY_SECRET` 环境变量
4. 更新 FC 触发器的 Basic Auth 密码
5. 更新 production `docker-compose.yml` 中的 `AVT_ALIYUN_SMS_RELAY_PASS`
6. 禁用旧 AccessKey
7. 重启 gateway 容器 + 验证短信和 captcha 仍工作

### 5.2 production 重新部署 risk_control.py

本轮改了 `risk_control.py` 的 fail-closed 行为，需要 docker cp + restart 到 production gateway。但考虑到密钥轮换应该先做，建议：**先轮换密钥 → 再部署新代码 → 一起验证。**

## 6. 停止状态

**已停止。** 等待 CodeX 审核。
