# GitNexus 支持 / 通知图

关联总图：`docs/graphs/GITNEXUS_PROJECT_GRAPH.md`

## 1. 范围

这张子图看的是“用户如何求助、AI 如何回答、何时转人工、公告与通知如何触达用户”，重点是：

- 帮助中心 / `SupportWidget` / `NotificationBell`
- `support_api` + `support_service`
- FAQ / plan facts / sanitized job context 回答链
- human handoff / presence / WeChat QR
- system announcements / live audiences / popup notifications
- pan backup / restore failure and token notification recipes
- admin compliance override notification
- support / notification write route CSRF guard
- visibility-aware polling for bell, support widget and admin heartbeat

## 2. 主图

```mermaid
graph TD
    AppShell["AppShell / Help / Bell / Popup / SupportWidget"] --> FrontApi["support.ts + notifications.ts"]
    HelpPage["/help"] --> FrontApi
    NotificationsPage["/notifications"] --> FrontApi

    FrontApi --> SupportAPI["/api/support/*"]
    FrontApi --> NotifAPI["/api/notifications*"]
    FrontApi --> Polling["usePollingTask visibility-aware polling"]

    SupportAPI --> SupportSvc["support_service.py"]
    SupportAPI --> CSRF["require_same_origin_state_change"]
    SupportSvc --> Policy["support_policy / route decision"]
    SupportSvc --> Knowledge["FAQ + plan facts + sanitized job context"]
    SupportSvc --> AI["support_ai / llm_registry"]
    SupportSvc --> Budget["support_budget"]
    SupportSvc --> Handoff["email / chatwoot / wechat_qr / wechat_kf / in_product"]
    SupportSvc --> Presence["support_presence / online-status"]

    AdminSupport["/admin/support + /admin/support/announcements"] --> AdminRouters["admin_support_api + system_announcements_service"]
    AdminRouters --> SupportSettings["settings / overview / presence / QR / handoff tickets"]
    AdminRouters --> Announcements["audience resolution + send / recall"]

    NotifAPI --> UserNotif["user_notifications"]
    NotifAPI --> CSRF
    Announcements --> UserNotif
    AuthPhone["complete-registration"] --> LiveDispatch["dispatch_announcements_for_new_user"]
    LiveDispatch --> UserNotif

    PanEvents["pan.backup.failed / pan.restore.failed / pan.token_revoked"] --> DispatchMap["notification_dispatch_map"]
    ComplianceEvent["content compliance admin override"] --> DispatchMap
    DispatchMap --> NotifSvc["notifications_service"]
    NotifSvc --> UserNotif

    UserNotif --> Bell["NotificationBell / unread count"]
    UserNotif --> Popup["NotificationPopupModal"]
    UserNotif --> Feed["NotificationsPage"]
    Polling --> Bell
    Polling --> SupportWidget["SupportWidget online status"]
    Polling --> AdminHeartbeat["admin support heartbeat"]
```

## 3. 当前最重要的结构认知

### 3.1 support 前门已经是正式产品面，不再是占位页或散落联系信息

- `frontend-next/src/components/app-shell.tsx` 现在直接挂了 `SupportWidget`
- `frontend-next/src/app/(app)/help/page.tsx` 已从“开发中”占位升级成真实帮助中心落地页
- `frontend-next/src/lib/api/support.ts` 已覆盖：
  - `getSupportConfig`
  - `createSupportConversation`
  - `sendSupportMessage`
  - `requestSupportHandoff`
  - `getOnlineStatus`
  - `listMyOpenConversations`

结论：支持已经进入主应用入口，而不是“用户自己找邮箱”。

### 3.2 `support_service.py` 是唯一决定一条消息怎么走的编排层

- 模块头明确写死三条规则：
  - 它是唯一决定“这条消息走 AI / template / handoff 哪条路”的地方
  - 所有路径都会写 `support_messages`
  - 所有路径都会写 `support_ai_usage`，即使没有真实 LLM 调用
- 代码入口把 `plan facts`、`FAQ search`、`sanitize_job_context_for_ai(job)`、`support_budget`、`support_policy` 串到一起

结论：support 不再是单个 prompt 或单个接口，而是正式 orchestrator。

### 3.3 support 的知识面是“FAQ + 套餐事实 + 用户自有任务上下文”

- `support_service.py` 会在用户拥有该 job 的前提下加载 job context
- 匿名用户与跨用户请求拿不到别人的任务上下文
- `support.ts` 的 `SupportSource` 已把可引用来源规范成：
  - `faq`
  - `plan_catalog`
  - `legal_page`
  - `job_status`
  - `template`
  - `notification`

结论：AI 客服能看的是受约束的业务知识与用户自有上下文，不是整个数据库。

### 3.4 人工接管已经有 presence、offline fallback、WeChat QR 等正式语义

- `support_api.py` 的 `/online-status` 会返回：
  - `online`
  - `online_count`
  - `has_wechat_qr`
  - `offline_message`
  - `handoff_offline_fallback_minutes`
- 前端类型里 handoff provider 已固定为：
  - `in_product`
  - `wechat_qr`
  - `email`
  - `chatwoot`
  - `wechat_kf`
- admin 支持页已经有 `PresenceConfigCard`、`WeChatQrCard`、`HandoffTicketsPanel`

结论：support 不是“全都交给 AI”，而是已经建模了在线人工与离线 fallback。

### 3.5 通知中心是用户可见投影，而不是任务状态真源

- `gateway/notifications_api.py` 提供：
  - 列表
  - unread count
  - mark read
  - archive
  - popup modal feed
- `frontend-next/src/app/(app)/notifications/page.tsx` 明确写着：
  - 它是 pipeline/system/support events 的 user-visible projection
  - 任务权威状态仍然在 job detail view

结论：通知平面解决的是“提醒与触达”，不是替代任务详情页。

### 3.6 系统公告现在可以直接扇出到通知中心，并支持新注册用户 live audience

- `system_announcements_service.py` 现在有 14 类 audience
- 其中 `for_new_registrations` 是 live audience
- `send_announcement(...)` 会把公告复制成 `UserNotification` 行
- `dispatch_announcements_for_new_user(...)` 会在新用户注册成功后补发所有仍处于激活状态的 live announcement
- popup 公告也会连同 `popup=true` 一起进入 feed

结论：系统公告、通知中心、注册后 onboarding 已经连成一条用户触达链。

### 3.7 Pan 与 admin compliance 已进入通知投影

- `notification_dispatch_map.py` 注册 `pan.token_revoked`、`pan.backup.failed`、`pan.restore.failed`，用于把网盘凭证和备份/恢复失败推到通知中心。
- Pan executor 会在失败路径调用 dispatch，错误原因经过 allowlist 后进入通知文案。
- Admin 内容合规命中 blocked 时不会阻断 pipeline，但会派发 warning/popup 通知，提醒管理员该任务有合规风险。

结论：通知中心现在覆盖运营公告、客服消息之外的运维风险事件。

### 3.8 支持和通知写路由受 CSRF 保护

- `support_api.py` 的 router 接入 `require_same_origin_state_change`，创建会话、发送消息、handoff 请求等 session 写操作需要同源。
- `admin_support_api.py` 的 admin router 也接入 same-origin guard，settings、presence、handoff close、公告发布/撤回等写操作不再只靠 session cookie。
- `notifications_api.py` 的用户通知 router 接入 same-origin guard，mark read、archive、popup acknowledge 等写操作需要合法 Origin/Referer。

结论：support/notification 是用户可操作面，排查 403 时要同时看 auth 和 CSRF origin，不要只查权限。

### 3.9 通知和支持轮询现在感知页面可见性

- `NotificationBell.tsx` 使用 `usePollingTask` 每 30 秒刷新 unread count；hidden tab 下由 hook 控制暂停/恢复，visibility 恢复时刷新。
- `SupportWidget.tsx` 的在线状态刷新也走 `usePollingTask`，只在浮窗打开时轮询。
- `useAdminHeartbeat.ts` 在 hidden 超过阈值时停止持续 heartbeat，恢复可见时重新同步。

结论：后台标签页里“没有持续刷新”现在是前端压力治理的一部分，不一定是 API 或通知链路故障。

## 4. 关键证据

- `gateway/support_api.py`
  - 会话 / 消息 / handoff / online-status / WeChat QR
- `gateway/support_service.py`
  - route decision orchestrator
  - knowledge + job context + budget + handoff
- `gateway/support_presence.py`
  - online presence
- `gateway/support_handoff.py`
  - human handoff
- `gateway/admin_support_api.py`
  - settings / overview / admin support surfaces
- `gateway/system_announcements_service.py`
  - audience resolver
  - send / recall
  - `for_new_registrations`
- `gateway/notifications_api.py`
  - bell / feed / popup API
  - CSRF-protected write routes
- `gateway/notifications_service.py`
  - event-driven notification rows
- `gateway/notification_dispatch_map.py`
  - pan failure / token revoked recipes
  - admin compliance override recipe
- `gateway/pan/backup_executor.py`
  - backup failure notification dispatch
- `gateway/pan/restore_executor.py`
  - restore failure notification dispatch
- `frontend-next/src/components/app-shell.tsx`
  - `SupportWidget`
  - `NotificationBell`
  - `NotificationPopupModal`
- `frontend-next/src/lib/react/usePollingTask.ts`
  - visibility-aware polling
- `frontend-next/src/components/support/useAdminHeartbeat.ts`
  - visibility-aware admin presence heartbeat
- `frontend-next/src/app/(app)/help/page.tsx`
  - help center landing page

## 5. 什么情况下优先读这张图

- 想改帮助中心、客服浮窗、通知中心
- 想判断 support 消息到底什么时候走 FAQ、什么时候走 LLM、什么时候强制转人工
- 想接入或修改 WeChat QR / email / chatwoot / wechat_kf handoff
- 想做系统公告、popup 触达、或新注册用户 onboarding 通知
- 想接入 pan.* 或 admin compliance 这类运维/风险通知
- 想排查 support / notification 写请求为什么被 CSRF 拦截
- 想排查通知铃铛、客服在线状态、admin heartbeat 为什么在后台标签页暂停
