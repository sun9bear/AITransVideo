# 前端轮询轻量治理计划（2026-05-21）

## 目标

减少前端重复轮询和无效请求，但保持当前 staged v2 迁移原则：轻量、可测试、可逆，不引入 TanStack Query / SWR 作为第一步。

## 当前事实

- 项目列表页不是无条件轮询；只有存在 active job 时才 4 秒刷新。
- `useBackgroundTask` 已有 terminal stop 与错误 backoff。
- `usePollingTask` 仍是简单 `setInterval` helper，缺少 in-flight guard、visibility pause 和请求去重。
- 通知、客服、后台任务、编辑页 profile inference 等轮询各自实现，频率和停止条件不完全一致。

## 治理原则

1. **先收敛 helper，不先引入框架**：避免为少量轮询治理增加全局依赖和迁移成本。
2. **轮询必须有状态条件**：只在 pending / running / active / drawer-open 等必要状态下运行。
3. **一个轮询点只允许一个 in-flight 请求**：避免慢请求堆积造成客户端竞态。
4. **页面不可见时降频或暂停**：后台标签页不应继续高频打 Gateway。
5. **终态立即停**：succeeded / failed / cancelled / closed 等终态不继续轮询。

## Phase 0：轮询点盘点

输出轮询 inventory：

| 位置 | 当前频率 | 触发条件 | 停止条件 | 风险 |
| --- | ---: | --- | --- | --- |
| `projects/page.tsx` | 4s | active job exists | no active job | 中 |
| `useBackgroundTask` | 2.5s/4s + backoff | pending/running | terminal | 低 |
| `usePollingTask` callers | caller-defined | helper enabled | helper disabled | 中 |
| notifications | 30s | mounted | unmounted | 低 |
| support | 5s/30s | widget/drawer state | closed/unmounted | 中 |
| edit profile inference | 4s | inferring speaker profile | ready/failed | 低 |

验收：

- 列出所有 `setInterval`、`setTimeout` polling loop、manual `while poll`。
- 每个轮询点都有明确 owner 和停止条件。

## Phase 1：增强 `usePollingTask`

在 `frontend-next/src/lib/react/usePollingTask.ts` 增加可选能力：

- `pauseWhenHidden?: boolean`
- `skipIfInFlight?: boolean`
- `runWhen?: () => boolean`
- `onError?: (err) => void`

默认建议：

- `pauseWhenHidden=true`
- `skipIfInFlight=true`
- 保持当前 `intervalMs=4000` 和 `immediate=true`，减少迁移冲击

行为：

- 请求未完成时不启动下一轮。
- `document.visibilityState === "hidden"` 时暂停或降频。
- `enabled=false` 时清理 timer。
- 组件 unmount 后不再 setState。

测试：

- fake timers 验证 interval。
- in-flight promise 未 resolve 时不会并发调用。
- hidden tab 时不调用或降频。
- enabled 从 true -> false 清理 timer。

## Phase 2：迁移高价值调用点

优先迁移：

1. `projects/page.tsx`：保留 active-job 条件，加 in-flight guard 与 hidden pause。
2. `NotificationBell.tsx`：后台标签页降频，用户打开通知页时即时刷新。
3. `SupportConversationPanel.tsx`：只在会话 open / handoff pending 时轮询。
4. 编辑页 profile inference：只在存在 inferring speaker 时轮询。

不在第一轮迁移：

- `useBackgroundTask`，它已经有 backoff 和 terminal stop，除非发现重复请求。
- 批量重合成的显式 `while poll`，先保留，后续单独整理。

## Phase 3：指标与回归

最小指标：

- active job 数量为 0 时，项目列表不再请求。
- 页面 hidden 60 秒内，项目列表请求数为 0 或显著降频。
- 慢请求情况下，同一轮询点并发请求数不超过 1。

可选观测：

- Gateway access log 中 `/job-api/jobs`、notifications、support 轮询频率下降。
- 前端错误日志中因竞态产生的 stale state warning 下降。

## 不做范围

- 不引入 TanStack Query / SWR。
- 不改 Gateway API 响应格式。
- 不把轮询改成 WebSocket / SSE。
- 不改变任务状态机。

## 推荐第一步

先做 Phase 0 inventory，再增强 `usePollingTask`。如果 inventory 显示轮询点继续扩散，再评估 Query/SWR，而不是现在提前引入。
