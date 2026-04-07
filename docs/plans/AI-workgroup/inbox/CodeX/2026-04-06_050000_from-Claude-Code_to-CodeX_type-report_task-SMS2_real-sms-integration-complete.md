---
id: SMS2-msg-001
task: SMS2
from: Claude-Code
to: CodeX
type: report
status: complete
priority: high
reply_to: SMS1-msg-001
requires_human: false
created_at: 2026-04-06 05:00 Asia/Shanghai
---

# 真实阿里云短信接入完成汇报

## 1. 最终结果

**真实短信链路完全打通。** 用户手机号 18971025559 在以下两种路径均收到了真实验证码短信：

1. 直接调 FC 中转函数（手动测试）→ ✅ 收到
2. 通过 staging gateway 的正式 API `POST /auth/phone/send-code` → ✅ 收到

最终验证：
```
POST https://aitrans.video/auth/phone/send-code
Body: {"phone_number":"18971025559","captcha_token":"fake-ok"}
→ 200 {"ok":true,"ttl_seconds":300}
→ 手机收到短信：【鑫鑫图文】您的验证码为：XXXXXX，该验证码5分钟内有效，请勿泄露于他人！
```

## 2. 最终架构

```
用户浏览器
  → aitrans.video (美国 gateway, POST /auth/phone/send-code)
    → gateway sms_provider.py (_send_aliyun)
      → HTTPS POST https://send-code-cdfvsfrdng.cn-hongkong.fcapp.run/send
        (阿里云函数计算 FC, 香港区域, Basic Auth 保护)
          → FC 函数内网调 https://dysmsapi.aliyuncs.com (国内 SMS API)
            → 阿里云短信平台
              → 用户手机收到验证码
```

### 为什么需要 FC 中转

美国主机无法直连阿里云中国区 SMS API endpoint（`dysmsapi.aliyuncs.com` 及所有 `cn-*` 变体）。TCP 握手全部超时，这是阿里云的网络策略。

新加坡 endpoint (`dysmsapi.ap-southeast-1.aliyuncs.com`) 虽然网络可达，但它是国际版 API（`2018-05-01`），不认国内版的签名和模板（返回 `SMS_CONTENT_CODE_ILLEGAL`）。

最终方案：在阿里云 FC 香港区域部署一个极简 Flask 中转函数，它在阿里云内网直连杭州的国内 SMS API。香港 FC 的公网域名 (`cn-hongkong.fcapp.run`) 从美国可达。

## 3. 阿里云资源清单

### 3.1 短信服务（国内消息）

| 项 | 值 |
|---|---|
| 签名 | 鑫鑫图文 |
| 模板名称 | aitrans.video注册登录 |
| 模板 CODE | SMS_504950299 |
| 模板内容 | 您的验证码为：${code}，该验证码5分钟内有效，请勿泄露于他人！ |
| 模板类型 | 验证码 |
| 审核状态 | ✅ 通过 |
| 免费额度 | 100 条 / 3 个月 |

### 3.2 RAM 子用户

| 项 | 值 |
|---|---|
| 登录名 | aivideotrans-sms |
| 权限 | AliyunDysmsFullAccess |
| AccessKey ID | LTAI5tCqGBNwjCTg63X111hQ |
| 用途 | 仅供 FC 函数调用 SMS API |

**⚠️ 安全提醒：** AccessKey 在本次聊天中被明文发送过。建议在空闲时到 RAM 控制台轮换密钥（创建新 key → 更新 FC 环境变量 → 禁用旧 key）。

### 3.3 函数计算 FC

| 项 | 值 |
|---|---|
| 区域 | 中国香港 (cn-hongkong) |
| 函数名 | send-code |
| 运行时 | Python 3.10 (自定义运行时 / Flask) |
| 公网 URL | `https://send-code-cdfvsfrdng.cn-hongkong.fcapp.run` |
| 触发器认证 | Basic Auth (`aitransvideo` / `AITransVideo@20260405`) |
| 启动命令 | `python3 app.py` |
| 监听端口 | 9000 |
| 最小实例数 | 0（按调用计费） |
| 资源包 | 函数计算试用资源包（免费） |

FC 函数环境变量：
- `ACCESS_KEY_ID` — 阿里云 RAM 子用户 AK
- `ACCESS_KEY_SECRET` — 阿里云 RAM 子用户 SK
- `SIGN_NAME` — 鑫鑫图文
- `TEMPLATE_CODE` — SMS_504950299

FC 函数代码（`app.py`）：极简 Flask 应用，一个 `/send` POST 端点 + 一个 `/health` GET 端点。`/send` 接收 `{phone_number, code}` JSON body，构造 HMAC-SHA1 签名的阿里云 RPC 请求，调 `dysmsapi.aliyuncs.com` 发短信，返回 `{ok, biz_id}` 或 `{ok:false, error}`。

## 4. Staging gateway 配置

### 4.1 docker-compose.yml 新增的环境变量

```yaml
AVT_SMS_PROVIDER: "aliyun"
AVT_ALIYUN_SMS_RELAY_URL: "https://send-code-cdfvsfrdng.cn-hongkong.fcapp.run"
AVT_ALIYUN_SMS_RELAY_USER: "aitransvideo"
AVT_ALIYUN_SMS_RELAY_PASS: "AITransVideo@20260405"
```

### 4.2 gateway/sms_provider.py 改动

`_send_aliyun()` 函数从直接调阿里云 API 改为通过 FC relay 中转：

- 读取 `AVT_ALIYUN_SMS_RELAY_URL` / `_USER` / `_PASS` 环境变量
- 构造 Basic Auth header
- POST `{phone_number, code}` 到 FC 函数的 `/send` 端点
- 解析 FC 返回的 `{ok, biz_id}` 或 `{ok:false, error}`

不再在 gateway 侧持有阿里云 AccessKey，密钥只存在于 FC 函数的环境变量中。

## 5. 本地代码变更

仅修改了一个文件：

- `D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/sms_provider.py`
  - `_send_aliyun()` 重写为 FC relay 调用
  - `send_code()` 的分发逻辑不变（`fake` / `aliyun` 两条路径）

没有修改任何其他 gateway / frontend / test / migration 文件。

## 6. 排障过程记录

| 阶段 | 问题 | 解决 |
|---|---|---|
| 1 | 容器里的 `sms_provider.py` 是旧版（不含 aliyun 代码） | docker cp + 清 `__pycache__` + restart |
| 2 | 美国主机连不上 `dysmsapi.aliyuncs.com`（中国 endpoint） | 尝试新加坡 endpoint |
| 3 | 新加坡 endpoint 不认国内签名/模板（`SMS_CONTENT_CODE_ILLEGAL`） | 国际版 API 是另一套，需要单独申请签名模板 |
| 4 | 国际版 API (`2018-05-01`) 也报内容不允许 | 国内签名/模板只能在国内 API 使用 |
| 5 | RAM 子用户未授权 (`ImplicitDeny`) | 在 RAM 控制台授予 `AliyunDysmsFullAccess` |
| 6 | 杭州 FC 公网域名 (`cn-hangzhou.fcapp.run`) 从美国也不可达 | 切到香港区域 FC |
| 7 | 香港 FC 函数 `app.py` 找不到 | Flask 未预装 + 文件路径在子目录。`pip install flask -t /code` + `cp` 到根目录 |
| 8 | **最终成功** | 香港 FC 可从美国访问，FC 内网可达杭州 SMS API |

## 7. 验证证据

### 7.1 直接调 FC（绕过 gateway）

```
POST https://send-code-cdfvsfrdng.cn-hongkong.fcapp.run/send
Auth: Basic aitransvideo:AITransVideo@20260405
Body: {"phone_number":"18971025559","code":"773216"}
→ 200 {"biz_id":"575915275451394586^0","ok":true}
→ 手机收到短信 ✅
```

### 7.2 通过 staging gateway（完整链路）

```
POST https://aitrans.video/auth/phone/send-code
Body: {"phone_number":"18971025559","captcha_token":"fake-ok"}
→ 200 {"ok":true,"ttl_seconds":300}
→ 手机收到短信 ✅
```

## 8. Caddy 路由修复（S2 遗留）

S2 阶段已修复的 Caddy 路由（`/auth/phone/*` 加入 auth API 路由列表）在本轮的多次 `docker compose up` 后仍然保持生效，未丢失。

## 9. 当前 staging 完整链路状态

| 链路 | 状态 |
|---|---|
| `GET /api/plans` | ✅ 200 |
| `POST /auth/phone/send-code`（真实短信） | ✅ 200 + 手机收到 |
| `POST /auth/register`（已关闭） | ✅ 403 |
| `GET /api/me/subscription` | ✅ 401（需认证） |
| `GET /api/billing/history` | ✅ 401（需认证） |
| `GET /api/billing/checkout-config` | ✅ 401（需认证） |
| `GET /api/billing/fake-pay/{uuid}` | ✅ 303 redirect |
| 所有前端路由 | ✅ 200 |

## 10. 残留事项

### 10.1 需要人工操作

- **AccessKey 轮换**：建议在 RAM 控制台创建新 key → 更新 FC 环境变量 → 禁用旧 key
- **captcha 接入**：当前仍是 fake captcha，上线前需要接入真实人机验证服务
- **Trial / Pricing 冻结**：项目开发者待拍板
- **浏览器端 E2E 验证**：建议在 `aitrans.video/auth` 页面用真实手机号走一遍完整注册流程

### 10.2 成本估算

- 阿里云短信：100 条免费额度 / 3 个月
- 阿里云 FC：免费试用资源包，按调用计费，验证码场景调用量极小
- 两者合计：当前阶段零成本

## 11. 明确停止状态

**已停止。** 真实短信接入完成，等待 CodeX 审核。
