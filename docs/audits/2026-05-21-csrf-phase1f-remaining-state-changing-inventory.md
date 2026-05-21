# CSRF Phase 1F：剩余状态变更入口复盘

## 结论

本轮补齐两个低风险 session 写入口：

- `POST /api/jobs/{job_id}/materials-pack`
- `POST /api/billing/orders`

补齐后，普通登录用户主路径的写入口已基本挂上 `require_same_origin_state_change`。剩余未挂 CSRF guard 的状态变更路由都被显式分类为豁免或待拆分评估，并由 `tests/test_csrf_remaining_route_inventory.py` 锁定。

## 本轮接入

| Endpoint | 分类 | 处理 |
| --- | --- | --- |
| `POST /api/jobs/{job_id}/materials-pack` | 登录用户兼容导出入口 | 接入 CSRF guard |
| `POST /api/billing/orders` | 登录用户创建支付订单 | 接入 CSRF guard |

## 仍未挂 guard 的清单

| Endpoint | 原因 |
| --- | --- |
| `POST /api/billing/fake-pay/{order_id}` | 支付/测试回调语义，不按 session CSRF 处理；Phase 1I 已加生产默认禁用 gate |
| `POST /api/billing/webhooks/{provider_name}` | Provider webhook，走签名/幂等模型 |
| `POST /api/internal/user-voices/*` | Internal API，走 internal key / loopback 保护 |
| `POST /internal/notifications/dispatch` | Internal API，走 internal key |
| `POST /job-api/jobs/{job_id}/source-metadata` | Internal API，已有 `_require_internal_access` |
| `POST /job-api/jobs/{job_id}/metering` | Internal API，已有 `_require_internal_access` |

## 下一步建议

1. 支付 webhook 继续走 provider signature / idempotency，不接入 session CSRF guard。

## Phase 1G 更新

Support anonymous visitor cookie 已在后续 Phase 1G 独立覆盖：

- `POST /api/support/conversations`
- `POST /api/support/conversations/{id}/messages`
- `POST /api/support/conversations/{id}/handoff`

这些入口不再属于剩余未挂 guard 清单。

## Phase 1H 更新

`/job-api/{path:path}` 已在后续 Phase 1H 覆盖：非 jobs 透明代理只发现 `GET /job-api/voice-library` 这类读路径仍在使用，前端写路径已经集中在 `/job-api/jobs/*` 或 `/gateway/*`。因此 catch-all 已接入 `require_same_origin_state_change`；该 helper 对 GET/OPTIONS no-op，仅对 PUT/PATCH/DELETE 拦截跨站来源。

## Phase 1I 更新

Fake payment 已加生产 gate：`AVT_ENV=production` 时，fake provider 默认 non-operational，`POST /api/billing/fake-pay/{order_id}` 返回 403，`GET /api/billing/fake-pay/{order_id}` 重定向回 billing error 状态；只有显式设置 `AVT_ENABLE_FAKE_PAYMENT=true` 才允许生产 smoke test。
