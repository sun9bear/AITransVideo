# CSRF Phase 1C：普通登录用户写接口优先覆盖

## 目标

在 Admin 写接口闭环后，把同一个 `require_same_origin_state_change` helper 扩展到普通登录用户的高频写接口。

本阶段仍不接入支付 webhook、internal API、匿名客服入口，也不修改 `SameSite=Lax`。

## 本次覆盖

- `user_voice_api.router`
  - 个人音色新增、更新、删除、探测、速度校准。
- `notifications_api.router`
  - 标记已读、归档、弹窗通知关闭。
- `background_task_api.router`
  - 创建导出/素材包后台任务。
- `main.py` 直接注册的 voice selection POST：
  - `POST /job-api/jobs/{job_id}/voice-clone`
  - `POST /job-api/jobs/{job_id}/voice-match`
  - `POST /job-api/jobs/{job_id}/voice-candidates`

## 明确不覆盖

- `user_voice_api.internal_router`
- `notifications_api.internal_router`
- 支付 webhook / provider callback
- 匿名客服会话入口
- Job 创建/删除等更宽的 `/job-api/jobs` 写面，留到下一批单独评估。

## 接入方式

- 有 `APIRouter` 的模块使用 router-level dependency。
- `voice_selection_api` 由 `main.py` 直接注册函数，因此在 `app.post(...)` 注册处加 dependency。
- helper 对 `GET` / `HEAD` / `OPTIONS` no-op，因此同一 router 的只读接口不受影响。

## 验收

- 新增 wiring 测试扫描本批 session user router 的写方法。
- 测试确认 internal router 没被误加 session CSRF guard。
- 回归 background task、notifications popup、user voice internal、voice selection 相关测试。
