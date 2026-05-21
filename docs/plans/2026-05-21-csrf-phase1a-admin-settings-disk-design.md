# CSRF Phase 1A：Admin settings / admin disk 小范围接入设计

## 目标

先给后台高风险写接口补上应用层 Origin / Referer 校验，形成可测试闭环；暂不调整 `SameSite=Lax`，也不引入全站 CSRF token 框架。

## Helper 语义

新增 `gateway/csrf.py::require_same_origin_state_change(request)`：

- 只处理 `POST` / `PUT` / `PATCH` / `DELETE`。
- `GET` / `HEAD` / `OPTIONS` 不拦截。
- 优先校验 `Origin`，缺失时回退到 `Referer`。
- 允许来源来自三类 origin：
  - 当前请求 public origin：`Host` / `X-Forwarded-Host` + `X-Forwarded-Proto`。
  - `AVT_CORS_ORIGINS` 对应的 `settings.cors_origins`。
  - `SITE_URL` / `NEXT_PUBLIC_SITE_URL` 环境变量。
- 校验失败统一返回 `403 csrf_origin_rejected`。

## 本次接入范围

只接入 Admin settings 和 Admin disk 的写接口：

- `POST /api/admin/settings`
- `POST /api/admin/review-prompts`
- `POST /api/admin/model-toggle`
- `POST /api/admin/review-prompts/restore`
- `DELETE /api/admin/review-prompts/history/{index}`
- `POST /api/admin/jobs/{job_id}/cancel`
- `POST /api/admin/jobs/{job_id}/delete`
- `PATCH /api/admin/users/{user_id}/entitlements`
- `POST /api/admin/disk/cleanup-orphans`
- `POST /api/admin/disk/cleanup-expired`
- `POST /api/admin/disk/resize-filesystem`

接入方式使用 FastAPI route-level dependency：

```python
@router.post("/settings", dependencies=[Depends(require_same_origin_state_change)])
```

这样不会改变现有 handler 签名，也不会影响已有直接调用 endpoint 函数的单元测试；运行时请求仍会先经过 CSRF dependency。

## 暂不接入范围

- 支付 webhook、内部 API：继续走签名 / internal key 机制。
- 公开只读接口：不加 CSRF guard。
- 其他 admin router：后续按同一 helper 分批接入。
- 全站 CSRF token：等 Origin / Referer guard 覆盖结果稳定后再评估。

## 验收

- helper 单测覆盖 same-origin、forwarded public origin、CORS allowlist、Referer fallback、跨源拒绝、缺失 Origin/Referer 拒绝、GET 不受影响。
- wiring 单测检查上述 Admin 写接口都挂载 `require_same_origin_state_change`。
- 既有 admin gate 扫描仍应通过，保证 `_require_admin` 未被替代或削弱。
