# CSRF Phase 1B：剩余 Admin 写接口覆盖

## 目标

在 Phase 1A 的 `require_same_origin_state_change` helper 基础上，把剩余 session 管理员写接口纳入同一 Origin / Referer 防线。

本阶段仍不修改 `SameSite=Lax`，也不引入全站 CSRF token。

## 接入方式

对 admin-only router 使用 router-level dependency：

```python
router = APIRouter(
    prefix="/api/admin/...",
    tags=["..."],
    dependencies=[Depends(require_same_origin_state_change)],
)
```

原因：

- helper 只拦截 `POST` / `PUT` / `PATCH` / `DELETE`。
- `GET` / `HEAD` / `OPTIONS` 会直接 no-op，因此不会影响只读页面。
- 不改 handler 签名，降低对既有单元测试和内部函数调用的扰动。

## 本次覆盖

- Pricing admin：保存草稿、发布定价。
- Voice catalog admin：CRUD、批量导入、验证、标签任务。
- Pan admin：connect、backup、restore、disconnect、soft-delete backup。
- Admin job monitor：日志 AI 分析。
- Admin support：客服设置、在线状态、二维码、公告、人工回复等后台写接口。

Read-only admin modules（cost、credits observability、traffic、S2 GET surfaces）不新增写接口；通用测试会在这些 router 后续出现写接口时强制要求挂 CSRF guard。

## 验收

- 通用 wiring 测试扫描所有 session admin router 的 `POST` / `PUT` / `PATCH` / `DELETE`，缺少 `require_same_origin_state_change` 即失败。
- Phase 1A helper 行为测试继续通过。
- Admin gate 扫描继续通过，确认 CSRF guard 没有替代 `_require_admin`。
