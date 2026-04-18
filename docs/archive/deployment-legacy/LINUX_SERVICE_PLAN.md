# LINUX_SERVICE_PLAN.md

# AIVideoTrans Linux 服务计划

## 1. 服务计划目标

本文件只定义 Linux 迁移阶段的最小服务运行方案。

目标是：

- 让当前稳定基线在 Linux 上可启动
- 可停止
- 可重启
- 可恢复
- 可验收

而不是引入更复杂的平台化基础设施。

---

## 2. 推荐运行方式

优先推荐：

- **Docker Engine**
- **Docker Compose（单机）**
- **systemd 管理 compose 项目**

原因：

1. 环境更容易复制
2. 比手工脚本更适合长期运行
3. 后续更利于迁移 / 回滚
4. 比直接上 Kubernetes 简单很多

---

## 3. 最小服务拆分

## 3.1 app service
职责：
- 启动 Web UI
- 启动 Job API
- 承载 process-backed runtime
- 访问数据目录：
  - `projects/`
  - `jobs/`
  - `runtime_logs/`

说明：
- 初期可以仍在一个 service 内承载 Web UI + Job API
- 不要求现阶段继续拆分更多内部服务

## 3.2 caddy service
职责：
- 对外暴露 HTTPS
- 处理 Basic Auth（若沿用当前模式）
- reverse proxy 到 app 中的 Web UI
- 写 access log

说明：
- 只反代 Web UI
- 不允许把 Job API 直接暴露出去

---

## 4. Compose 责任边界

如果使用 compose，建议：

### compose 负责
- app service 生命周期
- caddy service 生命周期
- 网络与 volume 连接
- 环境变量注入
- 容器重启策略

### app 自己负责
- Web UI / Job API 内部逻辑
- 当前任务运行
- review continue
- result-summary
- 白名单下载

---

## 5. systemd 责任边界

如果使用 systemd，建议只让它负责：

- 启动 compose 项目
- 停止 compose 项目
- 重启 compose 项目
- 开机自启

不要让 systemd 直接承载复杂业务逻辑。

---

## 6. 启动顺序建议

### 启动
1. 载入环境变量
2. 挂载数据目录
3. 启动 app service
4. 验证 Web UI / Job API 本机可达
5. 启动 caddy service
6. 验证 HTTPS 入口可达

### 停止
1. 先停止 caddy
2. 再停止 app
3. 保持 `projects/` / `jobs/` / `runtime_logs/` 不被清理

---

## 7. 健康检查建议

至少要有这些最小检查：

### app preflight
- 配置是否可读
- 数据目录是否存在
- 端口是否可绑定
- Job API / Web UI 是否可启动

### caddy preflight
- Caddyfile 是否可解析
- 反代目标是否配置正确
- 凭证环境变量是否存在
- access log 路径是否可写

### runtime health
- Web UI 本机可达
- Job API 本机可达
- HTTPS 入口可达
- Job API 不被公网暴露

---

## 8. 运行时日志与恢复

### 日志
至少要能快速定位：
- app 是否启动成功
- caddy 是否启动成功
- 哪个端口未起来
- 哪个服务退出了
- 当前任务是否还在跑

### 恢复
最小恢复路径应包括：
- 重启入口服务
- 重启 app 服务
- 重新读取现有 `projects/` / `jobs/`
- 不破坏现有真相源数据

---

## 9. 明确不做什么

- 不做 Kubernetes
- 不做多机
- 不做数据库
- 不做消息队列
- 不做多 worker
- 不做完整监控平台
- 不做完整 CI/CD
- 不做自动扩缩容
- 不做蓝绿部署

---

## 10. 一句话计划

**Linux 服务计划 = app + caddy 的最小双服务结构，Compose 管理运行，systemd 管理长期启动。**
