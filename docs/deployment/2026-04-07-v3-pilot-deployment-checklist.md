# V3 Pilot Deployment Checklist

> 适用阶段：V3 shadow pilot 部署（V2 仍是生产真值）
> 日期：2026-04-07
> 配套文档：[V3 Pilot Observability Runbook](../plans/2026-04-07-v3-pilot-observability-runbook.md)

---

## 1. 目标与边界

### 1.1 部署性质

本次部署的是 **V3 shadow pilot**，不是 credits truth cutover。

- V3 shadow 数据（buckets / ledger / metering）开始在生产环境写入和读取
- V2 quota / billing / entitlements **继续是生产真值**，不受影响
- credits 不参与任务执行 gating
- shadow 写入失败不影响 V2 主业务

### 1.2 本次部署不做什么

- 不切换 credits 为计费真值
- 不退役 V2 quota
- 不上线 Top-up 购买
- 不上线完整退款回滚
- 不改变定价

---

## 2. 上线前检查

### 2.1 环境变量 / 配置文件

| 检查项 | 验证方式 | 说明 |
|--------|----------|------|
| `/opt/aivideotrans/config/.env` 存在 | `test -f /opt/aivideotrans/config/.env` | 基础环境配置 |
| `DATABASE_URL` 已配置 | `.env` 中有 `DATABASE_URL=postgresql+asyncpg://...` | V3 表写入依赖 PG |
| `AUTH_REQUIRED=true` | `.env` 中确认 | credits API 需要认证 |
| admin 用户已存在 | `psql` 查 `SELECT id, email, role FROM users WHERE role='admin'` | admin summary 需要 |

### 2.2 Migration / Schema 前置条件

| 检查项 | 验证方式 |
|--------|----------|
| migration 009 已执行 | `docker exec aivideotrans-gateway alembic -c /opt/gateway/alembic.ini current` 显示 `009_credits_metering` |
| `credits_buckets` 表存在 | `psql -c "\dt credits_buckets"` |
| `credits_ledger` 表存在 | `psql -c "\dt credits_ledger"` |
| `jobs` 表有 V3 字段 | `psql -c "\d jobs"` 包含 `estimated_minutes`, `actual_minutes`, `metering_snapshot` |

如果 migration 未执行：
```bash
docker exec aivideotrans-gateway alembic -c /opt/gateway/alembic.ini upgrade head
```

### 2.3 服务前置条件

| 服务 | 验证方式 |
|------|----------|
| PostgreSQL 运行 | `docker inspect aivideotrans-postgres` → running |
| Gateway 运行 | `docker inspect aivideotrans-gateway` → running |
| App (Pipeline) 运行 | `docker inspect aivideotrans-app` → running |
| Caddy 运行 | `docker inspect caddy` → running |
| Next.js 前端已构建 | Caddy 能代理到前端 |

---

## 3. Staging Rollout 顺序

### 步骤

1. **代码同步**
   ```bash
   # 按现有 Via-154 脚本部署
   Upload-Via-154.cmd   # 上传代码到远程主机
   Deploy-Via-154.cmd   # 执行部署
   ```

2. **执行 migration**
   ```bash
   docker exec aivideotrans-gateway alembic -c /opt/gateway/alembic.ini upgrade head
   ```

3. **重启 Gateway**
   ```bash
   docker restart aivideotrans-gateway
   ```

4. **重启 App（Pipeline metering writeback）**
   ```bash
   docker restart aivideotrans-app
   ```

5. **重建前端**
   ```bash
   cd /opt/aivideotrans/frontend-next && npm run build
   # 或按 docker compose 配置重建
   ```

6. **运行 smoke checks**
   ```bash
   bash scripts/verify-gateway-deploy.sh
   ```

7. **手工验证**（见 §5）

### Staging 通过标准

- `verify-gateway-deploy.sh` 全部 ✅
- `GET /api/credits/estimate?minutes=1&service_mode=express` 返回有效 JSON
- admin 用户可访问 `GET /api/admin/credits/summary`
- 创建一个测试任务后，admin summary 的 `metering.total_jobs` 增加

---

## 4. Production Rollout 顺序

### 前提

- Staging 已通过上述全部检查
- admin summary 在 staging 确认 shadow 数据在写入

### 建议发布时段

- 低流量时段（如工作日 22:00 - 08:00）
- 避免在大量用户正在创建任务时部署

### 步骤

与 Staging 相同（§3），额外注意：

- 部署前确认当前无进行中的大批量任务
- 部署后 10 分钟内完成 smoke checks
- 如有异常，参考 §6 回滚口径

---

## 5. 部署后手工验证

### 5.1 公开端点

```bash
# Health check
curl -sf http://127.0.0.1:8880/gateway/health

# Credits estimate (无需登录)
curl -sf "http://127.0.0.1:8880/api/credits/estimate?minutes=5&service_mode=express" | python3 -m json.tool
# 预期：{"estimated_credits": 50, "minutes": 5.0, ...}
```

### 5.2 认证端点（需登录 session cookie）

```bash
# Credits 余额
curl -sf -b cookies.txt http://127.0.0.1:8880/api/me/credits | python3 -m json.tool
# 预期：{"total_available": N, "buckets": [...], ...}

# Credits ledger
curl -sf -b cookies.txt http://127.0.0.1:8880/api/me/credits-ledger | python3 -m json.tool
# 预期：{"entries": [...], "count": N}
```

### 5.3 Admin 端点

```bash
# Admin summary (需 admin 角色)
curl -sf -b admin_cookies.txt http://127.0.0.1:8880/api/admin/credits/summary | python3 -m json.tool
# 预期：包含 buckets, ledger, metering, reserve_capture_closeness, field_status
```

### 5.4 前端人工点击检查

| 页面 | 检查项 |
|------|--------|
| `/settings/billing` | 点数余额卡片可见，无 JS 报错 |
| `/translations/new` | 费用预估面板显示预估点数徽章 |
| `/translations/new` | 切换 express/studio 后点数徽章更新 |

---

## 6. 异常回滚口径

### 6.1 只影响 shadow，可继续观察

| 现象 | 影响 | 处理 |
|------|------|------|
| admin summary 某些计数为 0 | shadow 数据未写入 | 检查 Gateway 日志，不影响 V2 |
| `/api/me/credits` 返回空 buckets | lazy grant 未执行 | 检查 DB 连接，不影响 V2 |
| billing 页 credits 卡片未显示 | 前端 fetch 失败 | 不影响 V2 billing 功能 |
| metering_snapshot 部分字段缺失 | Pipeline writeback 未触发 | 等下一个任务完成后复查 |

### 6.2 需要立即处理

| 现象 | 影响 | 处理 |
|------|------|------|
| Gateway 启动失败 | 全部 API 不可用 | 回滚代码，`docker restart aivideotrans-gateway` |
| migration 执行失败 | V3 表未创建 | 检查 PG 连接和权限，手工修复 |
| 前端 build 失败 | 页面不可访问 | 使用上一次的构建产物，排查 build 错误 |
| V2 quota / billing 行为异常 | 生产真值受损 | **立即回滚代码到部署前版本** |

### 6.3 回滚方式

如需回滚：
1. 用上一次成功部署的代码版本重新部署
2. Gateway 和 App 容器重启
3. V3 表（credits_buckets / credits_ledger）可保留不删——它们不影响 V2
4. 前端用上一次的构建产物
