# REMOTE_WORKBENCH_DEPLOYMENT_PLAN

Last updated: 2026-03-18

## 目标部署形态

- 当前阶段只面向 `Windows 单机、自用、可公网访问但有最小安全边界` 的远程网页工作台。
- 现有仓库仍以本地 Python 进程为运行基础，不引入数据库、队列、多 worker 或容器编排。
- 公网只进入 Web UI；Job API 继续只给本机 Web UI 使用。

## 服务拓扑

推荐的最小拓扑：

`公网 HTTPS 入口 -> 本机 Web UI (127.0.0.1:8876) -> 本机 Job API (127.0.0.1:8877) -> process-backed runner -> projects/ + jobs/`

补充说明：

- `Web UI`
  - 当前默认地址：`127.0.0.1:8876`
  - 负责提交任务、轮询状态、review continue、展示结果摘要
- `Job API`
  - 当前默认地址：`127.0.0.1:8877`
  - 负责 `POST /jobs`、状态读取、日志读取、continue、result-summary、artifacts
- `control-panel`
  - 当前默认地址：`127.0.0.1:8765`
  - 不属于公网远程工作台入口，保持本机使用或按需启动

## localhost 绑定原则

- `Web UI`、`Job API`、`control-panel` 都继续绑定 `127.0.0.1`。
- 不把应用服务直接绑定到 `0.0.0.0`。
- 远程访问能力只通过最前面的公网入口转发到 `Web UI`。

## 公网入口位置

- 公网入口应位于应用前面，承担：
  - HTTPS 终止
  - 最小认证
  - 只把请求转发到 `127.0.0.1:8876`
- 公网入口不应直接转发到 `Job API` 或 `control-panel`。
- P2 当前已落地的单一入口方案是：
  - `Caddy` 作为公网 HTTPS + 最小认证 + reverse proxy 入口
  - 由 `scripts/run_remote_workbench_service.py public-entry` 前台运行
  - 由 `scripts/start_remote_workbench.ps1 -Service public-entry` 或 `-Service all` 启动

## 端口边界

- 公网可见：
  - 只保留 Caddy 的 HTTPS 入口端口
- 本机服务端口：
  - `Web UI`: `127.0.0.1:8876`
  - `Job API`: `127.0.0.1:8877`
  - `control-panel`: `127.0.0.1:8765`
- Windows 防火墙应明确阻断外部对 `8876`、`8877`、`8765` 的直连。
- 若使用 Caddy 自动证书，公网通常还需要让 `80/443` 到达 Caddy，而不是到达应用本机端口。

## Windows 上的启动 / 常驻 / 日志落点建议

### 启动方式

- 建议把远程工作台视为长期运行的本机服务壳，而不是交互式临时命令。
- 当前仓库内已提供一份最小运行配置：
  - `remote_workbench.local.json`
- 最小常驻建议：
  - 常驻 `Job API`
  - 常驻 `Web UI`
  - 常驻公网入口组件
  - `control-panel` 按需启动，不作为远程入口必需组件

### 常驻方式

- 优先采用 Windows 原生计划任务或等价的服务包装方式。
- 当前仓库内已提供最小启动骨架：
  - `scripts/start_remote_workbench.ps1`
  - `scripts/run_remote_workbench_service.py`
- P2 额外约定：
  - `public-entry` 也纳入同一套启动脚本
  - `remote_workbench.local.json` 中的 `public_entry` 段驱动 Caddy 入口
  - `public-entry` 在后台拉起前先做一次 preflight / check-only；若缺 `caddy.exe`、缺认证环境变量或 `Caddyfile validate` 失败，应直接停止并给出明确阻塞项
- 常驻方案应具备：
  - 开机或登录后自动启动
  - 失败后自动重启
  - 不依赖自动打开浏览器

### 日志落点

- 当前仓库已有的任务状态与事件日志继续保留在：
  - `jobs/<job_id>.json`
  - `jobs/<job_id>.events.jsonl`
- 项目结果继续保留在：
  - `projects/<project_name>/`
  - `projects/<project_name>/manifest.json`
- 另外建议新增一层部署级运行日志目录，例如：
  - `runtime_logs/web-ui.stdout.log`
  - `runtime_logs/job-api.stdout.log`
  - `runtime_logs/public-entry.log`
- 当前默认运行日志目录由 `remote_workbench.local.json` 中的 `runtime_logs.directory` 控制，默认值为 `runtime_logs`
- P2 当前已落地的公网入口文件还包括：
  - `runtime_logs/public-entry.Caddyfile`
  - `runtime_logs/public-entry.access.log`
  - `runtime_logs/public-entry.stdout.log`
  - `runtime_logs/public-entry.stderr.log`

## 明确不做什么

- 不做 Linux 迁移
- 不做多机部署
- 不做数据库 / 队列 / 多 worker
- 不把 Job API 作为公网服务直接暴露
- 不扩张成完整 Web MVP
- 不开始 Skill
- 不开始商业化或多租户方案
