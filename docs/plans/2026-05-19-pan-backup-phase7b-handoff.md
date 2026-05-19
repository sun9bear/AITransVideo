# Pan Backup Phase 7b — Frontend Session Handoff

> **新会话读这个文件,5 分钟内能续上 Phase 7b 前端实施。**

**Last session ended:** 2026-05-19(Phase 7a backend 8 endpoints + 32 tests 完成)
**Next phase:** Phase 7b — Next.js admin UI(T7.7-T7.12,6 个 task,~1 工日)

---

## 1. 必读文档

1. **本文件** — Phase 7b 起点 + API 契约
2. `docs/plans/2026-05-14-admin-pan-backup-implementation-plan.md` — Phase 7 task list(line 3059+)
3. `docs/plans/2026-05-13-admin-pan-backup-design.md` — 设计 spec(§6 API 列表 + UI 联动 §)
4. `CLAUDE.md`(项目根) — 项目硬约束 + Next.js 路由说明

---

## 2. 当前 git 状态(验证用)

```bash
git log --oneline -3
# 期望:
#   59e3b2d feat(pan-backup): Phase 7a admin_pan_api.py — 8 endpoints + 412 guard
#   047b0ff fix(pan-backup): refresh success-path stale guard + cleanup test artifacts
#   ...

git fetch origin
git rev-list --count HEAD..origin/main  # 应为 0
git rev-list --count origin/main..HEAD  # 应为 0
```

---

## 3. Phase 7a 已完成(后端,不要重做)

`gateway/pan/admin_api.py` + `tests/test_pan_admin_api.py`(commit `59e3b2d`):

| Endpoint | 方法 | 用途 | Phase 7b 哪个 task 消费 |
|---|---|---|---|
| `/api/admin/pan/status` | GET | 连接状态 + Baidu 配额 + last_refreshed_at | T7.8 dashboard |
| `/api/admin/pan/backups` | GET | 列 BackupRecord(?status=&user_id=&job_id=&limit=&offset=) | T7.9 backup list page |
| `/api/admin/pan/backups/{id}/manifest` | GET | 读单个 backup 的 manifest_json | T7.9 详情面板 |
| `/api/admin/pan/backups` | POST `{job_id}` | 单任务 backup enqueue | T7.10 "备份到网盘" 按钮 |
| `/api/admin/pan/backups/batch` | POST `{job_ids[]}` | 批量 enqueue(per-job 结果) | T7.10 "备份选中" 批量按钮 |
| `/api/admin/pan/restores` | POST `{job_id}` | restore enqueue | T7.11 "Restore" 按钮 |
| `/api/admin/pan/credentials` | DELETE | 断开连接(status='revoked') | T7.8 "Disconnect" 按钮 |
| `/api/admin/pan/backups/{id}` | DELETE | 软删 backup(含 412 spec §6 保护) | T7.9 删除按钮 |

OAuth flow 入口在 Phase 6 (`gateway/pan/auth.py`):

| Endpoint | 方法 | 用途 |
|---|---|---|
| `/api/admin/pan/connect` | POST | 302 → Baidu OAuth |
| `/api/admin/pan/callback` | GET | OAuth 回调,302 → `/admin/pan/dashboard` |

---

## 4. Phase 7b 任务清单

| Task | 内容 | 文件 |
|---|---|---|
| T7.7 | API client wrapper | `frontend-next/src/lib/api/pan.ts` |
| T7.8 | Pan 状态卡片 + 连接/断开按钮 | `frontend-next/src/app/(app)/admin/pan/dashboard/page.tsx` |
| T7.9 | BackupRecord list page(过滤 + 详情 + 删除) | `frontend-next/src/app/(app)/admin/pan/backups/page.tsx` |
| T7.10 | Workspace 列表页加 "备份到网盘" 单/多选按钮(条件:admin + pan connected + status=succeeded) | `frontend-next/src/app/workspace/page.tsx` 或对应组件 |
| T7.11 | archived 行 UI(灰底、badge、"Restore" 按钮) | workspace 列表行组件 |
| T7.12 | 状态过滤器加 "archived" 选项 | workspace 状态过滤组件 |

---

## 5. API 客户端契约(T7.7 应该 export 的方法)

```typescript
// frontend-next/src/lib/api/pan.ts

export type PanStatus = {
  connected: boolean;
  status: 'disconnected' | 'active' | 'revoked';
  scope: string | null;
  last_refreshed_at: string | null;  // ISO
  connected_at: string | null;
  quota: { total: number; used: number; free: number } | null;
  quota_error?: string;
};

export type BackupRecord = {
  id: string;            // UUID
  user_id: string;
  job_id: string;
  job_edit_generation: number;
  provider: string;       // 'baidu_pan'
  remote_path: string;
  size_bytes: number;
  sha256: string;
  md5: string;
  status: 'uploading' | 'uploaded' | 'failed' | 'restoring' | 'restored' | 'deleted';
  heartbeat_at: string | null;
  created_at: string | null;
  completed_at: string | null;
  error_message: string | null;
};

export type BackupListResponse = {
  items: BackupRecord[];
  total: number;
  limit: number;
  offset: number;
};

export type EnqueueResponse = {
  task_id: string;
  job_id: string;
  status: 'pending';
};

export type BatchEnqueueResponse = {
  succeeded: Array<{ job_id: string; task_id: string }>;
  failed: Array<{ job_id: string; reason: string }>;
};

// All endpoints use the existing fetch wrapper convention from
// `frontend-next/src/lib/api/*.ts` (cookie-based auth, base URL from env).

export async function getPanStatus(): Promise<PanStatus>;

export async function listBackups(params?: {
  status?: string[]; user_id?: string; job_id?: string;
  limit?: number; offset?: number;
}): Promise<BackupListResponse>;

export async function getBackupManifest(id: string): Promise<{
  backup_id: string; status: string; manifest: Record<string, any>;
}>;

export async function createBackup(jobId: string): Promise<EnqueueResponse>;
export async function createBackupBatch(jobIds: string[]): Promise<BatchEnqueueResponse>;
export async function createRestore(jobId: string): Promise<EnqueueResponse>;

export async function disconnectPanCredentials(): Promise<void>;  // 204
export async function deleteBackup(id: string): Promise<void>;     // 204 or 412
```

### Error handling

- 401: redirect to login (existing convention)
- 403: render "需要管理员权限" (admin-only routes)
- 412 from POST /backups, /backups/batch entries, /restores:
  - `detail` contains user-readable Chinese reason
  - Display in toast / inline alert
- 412 from DELETE /backups/{id}:
  - "唯一可恢复副本" — special-case modal: "先 restore 后再 delete"
- 5xx: generic "服务暂时不可用,请稍后重试"

---

## 6. 页面布局参考

### `/admin/pan/dashboard` (T7.8)

```
┌────────────────────────────────────────────────────────┐
│ 网盘备份                                                │
├────────────────────────────────────────────────────────┤
│                                                         │
│  连接状态: ● 已连接 (active)                            │
│  连接账户: 百度网盘                                      │
│  连接时间: 2026-05-12 14:23                            │
│  最近刷新: 2026-05-19 03:00                            │
│  授权范围: basic netdisk                                │
│                                                         │
│  配额: 已用 500GB / 2TB (25%)  ━━━━░░░░░░░░░░          │
│                                                         │
│  [断开连接]                                              │
│                                                         │
└────────────────────────────────────────────────────────┘
```

未连接状态:

```
┌────────────────────────────────────────────────────────┐
│ 网盘备份                                                │
├────────────────────────────────────────────────────────┤
│  尚未连接网盘                                            │
│                                                         │
│  [连接百度网盘]                                          │
└────────────────────────────────────────────────────────┘
```

`revoked` 状态: 红色横幅 "授权已失效,请重新连接" + [重新连接] 按钮(走同一个 OAuth flow,callback UPSERT 会自动覆盖 revoked → active)。

### `/admin/pan/backups` (T7.9)

- 状态过滤器(multiselect):uploading / uploaded / failed / restoring / restored / deleted
- 表格列:job_id / status / size / created_at / completed_at / 操作
- 操作:[查看 manifest] [删除]
- 删除前 confirm 弹窗;412 时改提示 "唯一副本,先 restore"

### Workspace 列表页改动 (T7.10-T7.12)

- 每行的操作菜单加 "备份到网盘"
  - 条件:`user.role === 'admin'` AND `panStatus.connected === true && panStatus.status === 'active'` AND `job.status === 'succeeded'`
  - 点击 → confirm → POST /backups → toast "已加入备份队列"
- 多选模式工具栏加 "备份选中"
  - 同条件,但允许 status='succeeded' 的子集
  - POST /backups/batch → 显示 succeeded/failed 分组
- archived 行:
  - 行底色变浅灰
  - status 列加 "已归档" badge
  - 操作菜单加 "Restore"
  - 点击 → confirm → POST /restores → toast
- 状态过滤器加 "archived" 选项(目前可能没有)

---

## 7. 路由约定

Next.js App Router 在 `frontend-next/src/app/`:
- `(app)/` 是 route group(不出现在 URL):需要登录的页面分组
- `(app)/admin/` 下的页面:admin 路由
- URL `https://aitrans.video/admin/pan/dashboard` ↔ 文件 `app/(app)/admin/pan/dashboard/page.tsx`

OAuth callback 302 目标 `/admin/pan/dashboard` 已经在 Phase 6 写死,与此目录结构匹配。

---

## 8. 测试策略

Next.js 单元测试约定:看 `frontend-next/` 里现有的 `*.test.ts` 文件参考。E2E 主要靠 Playwright(在 `playwright/` 目录)。

对 Phase 7b,推荐:
- T7.7 API client:`*.test.ts` 单元测试 fetch wrapper 的请求 shape + 错误处理
- T7.8 dashboard:可以单测 React 组件,或直接 Playwright 验证连接/断开 flow
- T7.9 list:单测 filter logic + table rendering
- T7.10-T7.12:Playwright 走 admin login → succeeded job → "备份" → 列表显示 archived → restore

---

## 9. ⚠️ 注意事项

### 9.1 不要绕过后端契约

后端 `/api/admin/pan/*` 已经完整实现 + 测试覆盖。Phase 7b **只做 UI 消费**,不要在前端 reimplement 状态机或省略 412 处理。

### 9.2 OAuth 流程不要拦截

POST `/api/admin/pan/connect` 返 302 直接跳 Baidu。前端不要用 fetch 拦截(fetch 默认会 follow redirect 但跨域 OAuth 会破)。改用 `window.location.href = '/api/admin/pan/connect'` 直接导航,Gateway 302 浏览器 follow,Baidu 授权页可见,回到 callback,callback 302 到 dashboard。

### 9.3 connection state polling

dashboard 进入时 GET /status 一次;OAuth callback 回到 dashboard 后,前端不需要主动 poll —— callback 已经在后端写入 PanCredentials,GET /status 会立刻看到。

### 9.4 设计 spec 的 UI 联动可参考

`docs/plans/2026-05-13-admin-pan-backup-design.md` §"任务列表 UI 联动" + §"通知" 有更详细的设计要求。

---

## 10. 开场 prompt 模板

```
继续 admin pan backup implementation,从 Phase 7b 开始。

读 docs/plans/2026-05-19-pan-backup-phase7b-handoff.md 全文,验证
git state(§2),然后按 §4 的 6 个 task 顺序做:T7.7 API client →
T7.8 dashboard → T7.9 list → T7.10 backup buttons → T7.11 archived
UI → T7.12 status filter。

API 契约见 §5。每完成一个 task 单独 commit。Phase 7b 完成后 batch push。

工作目录已经在 main 分支,不要开 worktree。
```

End of handoff.
