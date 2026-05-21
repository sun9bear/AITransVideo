# CSRF Phase 1G：Support anonymous visitor cookie 覆盖

## 目标

客服入口同时支持登录用户和匿名访客。匿名访客使用独立 cookie `avt_support_anon`，它不是登录 session，不应混入普通用户 session CSRF 覆盖组。

本阶段为客服 visitor-cookie 写入口单独接入同源 guard，继续复用 `require_same_origin_state_change` 的 Origin / Referer 语义。

## 覆盖范围

`support_api.router` 使用 router-level dependency：

```python
router = APIRouter(
    prefix="/api/support",
    tags=["support"],
    dependencies=[Depends(require_same_origin_state_change)],
)
```

实际受影响的状态变更入口：

- `POST /api/support/conversations`
- `POST /api/support/conversations/{conversation_id}/messages`
- `POST /api/support/conversations/{conversation_id}/handoff`

GET 路由也会经过 dependency，但 helper 对 `GET` no-op，因此客服配置、在线状态、二维码和会话读取不改变行为。

## 不覆盖范围

- Admin support router 已在 Admin CSRF 阶段覆盖。
- Internal notifications dispatch 继续走 internal key。
- 支付 webhook / external callback 继续不走 session CSRF guard。

## 验收

- `tests/test_support_csrf_guard_wiring.py` 锁住 support visitor-cookie 写入口已挂 guard。
- `tests/test_csrf_remaining_route_inventory.py` 不再把 support POST 列为未覆盖剩余项。
- Support 相关源码/服务层回归继续通过。
