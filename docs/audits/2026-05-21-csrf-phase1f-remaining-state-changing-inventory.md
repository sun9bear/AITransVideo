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
| `POST /api/billing/fake-pay/{order_id}` | 支付/测试回调语义，不按 session CSRF 处理 |
| `POST /api/billing/webhooks/{provider_name}` | Provider webhook，走签名/幂等模型 |
| `POST /api/internal/user-voices/*` | Internal API，走 internal key / loopback 保护 |
| `POST /internal/notifications/dispatch` | Internal API，走 internal key |
| `POST /job-api/jobs/{job_id}/source-metadata` | Internal API，已有 `_require_internal_access` |
| `POST /job-api/jobs/{job_id}/metering` | Internal API，已有 `_require_internal_access` |
| `POST /api/support/conversations` | 匿名客服入口，使用独立 visitor cookie |
| `POST /api/support/conversations/{id}/messages` | 匿名/登录客服会话混合入口，需独立评估 visitor-cookie CSRF 策略 |
| `POST /api/support/conversations/{id}/handoff` | 匿名/登录客服会话混合入口，需独立评估 visitor-cookie CSRF 策略 |
| `PUT/PATCH/DELETE /job-api/{path:path}` | 非 jobs 透明代理，需 subpath-level inventory 后再决定 |

## 下一步建议

1. 单独做 Support anonymous visitor cookie 的 CSRF 策略，不把它和登录 session 混在一起。
2. 单独盘点 `/job-api/{path:path}` 当前实际可达 subpath，再决定是加 guard、拆路由，还是保留代理豁免。
3. 支付 webhook 继续走 provider signature / idempotency，不接入 session CSRF guard。
