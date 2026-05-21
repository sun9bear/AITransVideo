# CSRF Phase 1E：上传与 Job subresource 收尾覆盖

## 目标

补齐普通登录用户主路径里剩余的两个高价值写入口：

- `POST /gateway/upload-video`
- `POST /job-api/jobs/{job_id}/{subpath:path}`

继续复用 `require_same_origin_state_change`，不引入 token，不修改 `SameSite=Lax`。

## 本次覆盖

`gateway/main.py`：

- `POST /gateway/upload-video`
- `POST /job-api/jobs/{job_id}/{subpath:path}`

`/job-api/jobs/{job_id}/{subpath:path}` 同时服务 `GET` 与 `POST`。Route-level dependency 会在两种方法上执行，但 helper 对 `GET` no-op，因此只改变 POST 的跨源行为。

## 明确不覆盖

- `POST /job-api/jobs/{job_id}/source-metadata`
- `POST /job-api/jobs/{job_id}/metering`

这两个是 internal-key 路径，继续走 `_require_internal_access`。

- `/job-api/{path:path}` 非 jobs 代理 catch-all。

该路由包含更宽的透明代理语义，需先做 endpoint inventory 再决定是否接入或分拆。

## 验收

- Direct main route wiring 测试覆盖上传和 job subresource POST。
- 既有 Gateway route coverage 继续证明路由不会落入错误 catch-all。
- 上传 handler 轻量 TestClient 测试继续通过。
- Job subresource/Jianying 相关直接 handler 测试继续通过。
