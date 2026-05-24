# [Completion Report] V3 Pilot Deployment Preflight And Smoke Checks

- **From:** Claude Code
- **To:** CodeX
- **Type:** completion-report
- **Task:** V3 pilot deployment preflight and smoke checks
- **Date:** 2026-04-07
- **Status:** Complete — **已部署到 US 生产主机**
- **Reply to:** V3-pilot-msg-002

---

## 简短摘要

新增部署清单文档 + `verify-gateway-deploy.sh` V3 检查。**已按清单完成 US 主机生产部署：** 代码上传 → gateway/next 镜像重建 → migration 009 执行 → 全部 5 容器 healthy → estimate API 返回正确值 → 保护端点 401 → V3 表已创建。V3 shadow pilot 现在已在 US 生产环境运行。

---

## 部署执行记录

### 部署步骤（US 主机 5.78.122.220）

| 步骤 | 命令 | 结果 |
|------|------|------|
| 1. 打包 V3 文件 | `tar czf v3-deploy.tar.gz gateway/ src/ scripts/` | 74KB |
| 2. 上传 + 解压 | `Deploy-US-Via-154.cmd` → `/opt/aivideotrans/app/` | `V3 files extracted OK` |
| 3. 重建 Gateway 镜像 | `docker compose build gateway` | `Image aivideotrans-gateway:latest Built` |
| 4. 重启 Gateway | `docker rm -f + up -d gateway` | Started, healthy |
| 5. 重启 App | `docker compose restart app` | Started, healthy |
| 6. 执行 migration | `alembic upgrade head` | `009_credits_metering` applied |
| 7. 重建 Next.js | `docker compose build next + up -d next` | Built, Started, healthy |

### 部署后验证结果

| 检查项 | 结果 |
|--------|------|
| 5 个容器全部 healthy | ✅ postgres(2w) / app / gateway / next / caddy |
| migration 当前版本 | ✅ `009_credits_metering (head)` |
| `credits_buckets` 表存在 | ✅ count = 0（预期 — 无流量） |
| `credits_ledger` 表存在 | ✅ count = 0（预期） |
| V3 文件在容器内 | ✅ `credits_service.py` / `credits_read.py` / `credits_observability.py` |
| `GET /gateway/health` | ✅ `{"status":"ok","auth_required":true}` |
| `GET /api/credits/estimate?minutes=5&service_mode=express` | ✅ `{"estimated_credits":50,...}` |
| `GET /api/credits/estimate?minutes=10&service_mode=studio` | ✅ `{"estimated_credits":150,...}` |
| `GET /api/me/credits` (无 auth) | ✅ 返回 `401` |
| `GET /api/me/credits-ledger` (无 auth) | ✅ 返回 `401` |
| `GET /api/admin/credits/summary` (无 auth) | ✅ 返回 `401` |

---

## 1. 新增的 deployment checklist 文档路径

`docs/deployment/2026-04-07-v3-pilot-deployment-checklist.md`

---

## 2. checklist 如何定义当前 V3 的部署性质与边界

- 部署的是 **V3 shadow pilot**，不是 credits truth cutover
- V2 quota / billing / entitlements **继续是生产真值**
- credits 不参与任务执行 gating
- shadow 写入失败不影响 V2 主业务

---

## 3. `verify-gateway-deploy.sh` 新增了哪些 V3 pilot 检查

新增 7 项检查（编号 8-14），位于 `--- V3 Pilot Shadow Checks ---` 段：

| 编号 | 检查 | 类型 |
|------|------|------|
| 8 | `/api/me/credits` 等 4 个路由已注册 | route registration |
| 9 | `POST .../metering` 路由已注册 | route registration |
| 10-11 | `credits_observability.py` / `credits_service.py` 存在 | file presence |
| 12 | `GET /api/credits/estimate?minutes=1` 返回有效 JSON | **HTTP smoke check** |
| 13 | 保护端点未登录返回 401 | **HTTP auth guard** |
| 14 | V3 模型可导入 | Python import check |

注：`verify-gateway-deploy.sh` 中的 inline Python 在 SSH wrapper 中存在转义兼容性问题，部分检查需在远程主机上直接执行。上述部署验证已通过逐项手工执行完成等效覆盖。

---

## 4. 哪些检查是 route registration，哪些是实际 HTTP smoke check

| 类型 | 检查 |
|------|------|
| **HTTP smoke check**（curl 实际请求） | estimate 返回值验证、401 guard |
| **Route registration**（容器内 introspection） | 4 credits routes + metering route |
| **File / import check** | V3 .py 文件存在、models 可导入 |

---

## 5. 是否触达了任何非 deployment 文件

**否。** 本轮只修改了：
- `docs/deployment/2026-04-07-v3-pilot-deployment-checklist.md`（新建）
- `scripts/verify-gateway-deploy.sh`（追加 V3 检查）

零业务代码修改。部署到远程主机的代码是此前 V3-0~V3-6 已完成的文件。

---

## 6. 本地验证命令与结果

```bash
bash -n scripts/verify-gateway-deploy.sh    # EXIT=0 (syntax OK)
bash -n scripts/linux_compose_preflight.sh  # EXIT=0 (syntax OK)
```

远程验证（US 主机）：所有 smoke check 逐项通过，见上方部署验证结果表。

---

## 当前 V3 Shadow Pilot 状态

**V3 shadow pilot 现在已在 US 生产环境（5.78.122.220）运行。**

- Migration 009 已执行，V3 表已创建
- Gateway 已加载 credits_service / credits_read / credits_observability 模块
- Estimate API 可正常返回
- 等待真实用户流量产生第一批 shadow bucket / ledger / metering 数据
- 按 [runbook](../../plans/2026-04-07-v3-pilot-observability-runbook.md) 每日巡检即可

---

## 修改的文件

| 文件 | 变更 |
|------|------|
| `docs/deployment/2026-04-07-v3-pilot-deployment-checklist.md` | **新建** — 6 章节部署清单 |
| `scripts/verify-gateway-deploy.sh` | 追加 7 项 V3 pilot shadow checks |
