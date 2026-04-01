# LINUX_DEPLOYMENT_BASELINE.md

# AIVideoTrans Linux 部署基线

## 1. 目标环境

建议的唯一目标环境：

- **OS**：Ubuntu Server 24.04 LTS
- **部署形态**：Linux 单机
- **长期运行方式**：systemd + Docker Compose（推荐）
- **公网入口**：Caddy
- **对外暴露**：仅 HTTPS (Caddy -> Gateway + Next.js)
- **内部使用**：Job API、Gateway、PostgreSQL 仅容器内网 / 本机访问

---

## 2. 推荐主机规格

### 最低可用
- 4 vCPU
- 16 GB RAM
- 200 GB SSD

### 更稳推荐
- 8 vCPU
- 16–32 GB RAM
- 300–500 GB SSD

说明：
- 迁移阶段不建议为了省资源压到过低规格
- 真正吃资源的是音视频处理与项目文件，不是 OS 本身

---

## 3. 目录结构建议

建议在 Linux 主机上收口为：

```text
/opt/aivideotrans/
  app/                  # 应用代码
  config/               # 环境配置
  data/
    projects/           # 项目目录真相源
    jobs/               # job records / events
    runtime_logs/       # 运行日志
  caddy/
    Caddyfile           # 入口配置
  scripts/
```

### 说明
- `projects/` 与 `jobs/` 继续保持真相源地位
- `runtime_logs/` 继续作为运行日志目录
- 配置与代码、数据分离

---

## 4. 服务拓扑

建议最小服务拓扑：

### 4.1 app service
职责：
- 提供 Job API (8877)
- 执行 process-backed runtime
- 读写 `projects/` / `jobs/` / `runtime_logs/`

### 4.2 gateway service
职责：
- 统一 API 入口 (8880)
- 认证（session-based）
- 路由代理到 Job API
- 任务拦截与计划配额

### 4.3 frontend service (Next.js)
职责：
- 前端页面 (3000)

### 4.4 caddy service
职责：
- HTTPS 入口
- reverse proxy 到 Gateway (API) 和 Next.js (页面)
- access log

> 注：Web UI (8876) 已废弃，不再作为独立服务。

---

## 5. 端口边界

建议保持以下约束：

### 对外暴露
- `443`：Caddy HTTPS 入口

### 本机 / 容器内网
- `8880`：Gateway（统一 API 入口）
- `8877`：Job API
- `3000`：Next.js 前端
- `5432`：PostgreSQL

### 防火墙原则
- 仅开放 `443`
- `8880` / `8877` / `3000` / `5432` 不直接对公网开放

---

## 6. 配置契约

建议配置至少包含：

- Gateway 监听地址与端口 (8880)
- Job API 监听地址与端口 (8877)
- Next.js 前端监听端口 (3000)
- public entry 配置
- runtime log 目录
- Basic Auth 用户名与哈希（通过环境变量）
- 项目与任务数据目录

---

## 7. 日志基线

至少应有两类日志：

### 7.1 runtime logs
- app stdout/stderr
- job-api stdout/stderr
- gateway stdout/stderr
- frontend stdout/stderr

### 7.2 access logs
- Caddy access log
- 记录：
  - 请求时间
  - 路径
  - 状态码
  - host
  - 认证用户标识（若可安全记录）

---

## 8. 数据边界

### 必须保留
- `projects/`
- `jobs/`
- `runtime_logs/`

### 不应改变
- Job API 仍不直接暴露公网
- 白名单下载仍以 manifest-derived stable keys 为准
- review_state / project_state / manifest 仍保持各自真相源职责

---

## 9. 部署原则

1. 先保行为一致，再谈内部优化
2. 先让 Linux 上“能重复部署、能真实跑通”
3. 不在迁移阶段扩张功能面
4. 不让前端开发与迁移耦合

---

## 10. 一句话基线

**Linux 部署基线 = Ubuntu Server 24.04 LTS + 单机 + app + gateway + frontend + caddy + PostgreSQL + 仅 HTTPS 公网入口。**
