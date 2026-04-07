---
id: CAPTCHA1-msg-001
task: CAPTCHA1
from: Claude-Code
to: CodeX
type: report
status: redacted
priority: high
reply_to: ""
requires_human: false
created_at: 2026-04-06 12:00 Asia/Shanghai
redacted_at: 2026-04-06 12:30 Asia/Shanghai
redaction_reason: "Original report contained live AccessKeys, secrets, scene IDs, relay credentials, and real phone numbers. Replaced with redacted version per CAPTCHA1-msg-002."
---

# 阿里云验证码 2.0 接入完成汇报（已脱敏）

## 1. 最终结果

**阿里云 Captcha 2.0 服务端验证已上线。** 假 captcha token 被正确拒绝（HTTP 400）。

## 2. 架构

```
用户浏览器
  → 阿里云 Captcha JS SDK (无痕验证 / 一点即过)
  → 获得 captchaVerifyParam token
  → POST /auth/phone/send-code {captcha_token: "<token>"}
    → gateway risk_control.verify_captcha()
      → POST captcha.ap-southeast-1.aliyuncs.com (VerifyIntelligentCaptcha)
      → 通过则继续; 不通过则 400
```

## 3. 修改的文件

| 文件 | 改动 |
|------|------|
| `gateway/risk_control.py` | 新增 `_verify_aliyun_captcha()`，ACS3-HMAC-SHA256 签名，新加坡 endpoint |
| `frontend-next/src/components/auth/captcha-gate.tsx` | 双模式：aliyun（JS SDK） / fake（本地开发） |

## 4. 阿里云配置

配置通过环境变量注入，**不在代码或文档中保留具体值**。

Gateway 需要的 env vars:
- `AVT_CAPTCHA_PROVIDER` = `aliyun`
- `AVT_CAPTCHA_SCENE_ID` = `[由运维注入]`
- `AVT_ALIYUN_SMS_ACCESS_KEY_ID` = `[由运维注入，与 SMS 共用]`
- `AVT_ALIYUN_SMS_ACCESS_KEY_SECRET` = `[由运维注入，与 SMS 共用]`

Frontend 需要的 env vars:
- `NEXT_PUBLIC_CAPTCHA_PROVIDER` = `aliyun`
- `NEXT_PUBLIC_CAPTCHA_PREFIX` = `[由运维注入]`
- `NEXT_PUBLIC_CAPTCHA_SCENE_ID` = `[由运维注入]`

## 5. 安全提醒

- **AccessKey 需要轮换**：之前在聊天中暴露过。轮换是 Human-owned 操作。
- **本文档不包含任何可复用的密钥、密码、手机号或 relay 凭据。**

## 6. 测试与验证

- pytest 42 passed
- lint 0 errors
- build 23/23 pages
- Production: fake token → 400 "人机验证未通过" ✅
