# 部署态运行时验证 — 2026-04-03

> 本文档记录当前部署态事实，作为后续 Sprint 的输入依据。
> 验证范围：本地开发环境 + US 远程服务器（5.78.122.220）。

## 1. 执行环境

| 环境 | 说明 |
|------|------|
| 本地开发 | Windows 10 IoT Enterprise LTSC 2021, Python 3.12.13 (uv managed), 无本地 Docker |
| 远程 US | Linux, Docker Compose, 通过 Via-154 跳板机 SSH 访问 |
| 当前分支 | `codex/review-guidelines` |
| 工作区状态 | 本文档写入前 `git status --short` 显示 2 个 untracked 文件（`docs/plans/2026-04-03-post-stabilization-short-sprint-plan.md`、`uv.lock`），无 modified 文件。本文档自身写入后会新增 1 个 untracked 文件 |

## 2. 容器/服务拓扑（US 远程服务器）

命令：`docker compose ps --format 'table {{.Name}}\t{{.Status}}\t{{.Ports}}'`

| 服务 | 状态 | 说明 |
|------|------|------|
| `aivideotrans-app` | Up 36 minutes (healthy) | Python 后端 + Job API |
| `aivideotrans-gateway` | Up 4 hours (healthy) | FastAPI Gateway |
| `aivideotrans-next` | Up 4 hours (healthy) | Next.js 前端 |
| `aivideotrans-postgres` | Up 10 days (healthy) | PostgreSQL 16 |

**结论：** 4 个服务全部 healthy，与 `docs/QUICKSTART.md` 描述的拓扑一致。

## 3. 代码部署方式

命令：`docker inspect aivideotrans-app --format '{{json .Mounts}}'`

app 容器当前使用 **bind mount 模式**（开发期代码热更新）：

| 主机路径 | 容器路径 | 类型 |
|---------|---------|------|
| `/opt/aivideotrans/app/src` | `/opt/aivideotrans/app/src` | bind (rw) |
| `/opt/aivideotrans/app/main.py` | `/opt/aivideotrans/app/main.py` | bind (rw) |
| `/opt/aivideotrans/app/scripts` | `/opt/aivideotrans/app/scripts` | bind (rw) |
| `/opt/aivideotrans/config` | `/opt/aivideotrans/config` | bind (rw) |
| `/opt/aivideotrans/data/projects` | `/opt/aivideotrans/app/projects` | bind (rw) |
| `/opt/aivideotrans/data/jobs` | `/opt/aivideotrans/app/jobs` | bind (rw) |
| `/opt/aivideotrans/data/runtime_logs` | `/opt/aivideotrans/data/runtime_logs` | bind (rw) |

**结论：** `src/`、`main.py`、`scripts/` 均为 bind mount。主机修改代码后 `docker restart aivideotrans-app` 即可生效，不需要 rebuild 镜像。

**注意：** `Dockerfile` 和 `pyproject.toml` 不通过 bind mount 生效。Task 2 的 Dockerfile 改动（`pip install --no-cache-dir .`）只在下次 `docker compose build app` 时才会影响镜像。当前镜像仍使用旧的内联 pip install 方式安装了运行时依赖，但由于 bind mount 覆盖了 `src/`，稳定化计划的代码变更已生效。

## 4. 当前可验证的运行时入口

### 4.1 本地 CLI

命令：`python main.py --help`

结果：Usage 正常打印，exit code 1（pre-existing SystemExit 行为）。与 accepted baseline 一致。

### 4.2 远程 app 容器日志

命令：`docker compose logs --tail 20 app`

关键日志行：
```
App health passed: Job API http://127.0.0.1:8877
Job API binding: http://127.0.0.1:8877
Projects dir writable: /opt/aivideotrans/app/projects
Jobs dir writable: /opt/aivideotrans/app/jobs
```

**结论：** Job API 正常绑定 8877 端口，健康检查通过。

### 4.3 远程 gateway

`docker compose ps` 显示 gateway 为 healthy。gateway 日志通过 SSH 抓取时因 stderr 输出导致脚本报 exit code 1，但健康检查已通过，不影响结论。

### 4.4 历史支持性事实（非本轮验证）

以下事实来自上一轮部署操作（本会话早期），不是本轮 Task 1 重新执行的命令。本轮未重验容器内 Python import / symbol existence。

上一轮部署时通过 `docker cp` + `docker exec` 在容器内运行 Python 验证脚本，观察到：

- `SemanticBlock.alignment_method` 和 `needs_review` 字段可用
- `TTSGenerator._resolve_provider_decision` 方法存在
- `_ProcessOutputAlignedBlock` 已从 `process.py` 中删除

这些观察是在 app 容器最近一次 restart 后采集的。本轮 Task 1 未重新验证这些事实，仅作为历史支持性参考。

## 5. 无法验证的部分与原因

| 项目 | 无法验证原因 | 影响 |
|------|-------------|------|
| 端到端 TTS 路由（真实 cosyvoice/minimax 调用） | AGENTS.md 约束不允许引入真实外部 API | 不阻塞本 Sprint；运行时路由证据已由 mocked 测试覆盖 |
| Dockerfile 改造后的 rebuild 效果 | 当前 app 使用 bind mount 模式，Dockerfile 改动未通过 rebuild 验证 | 不阻塞当前开发流程；切回镜像不可变模式时需要 rebuild 验证 |
| gateway 日志详细内容 | SSH 脚本在 gateway stderr 输出时报错，未能完整抓取日志 | 不阻塞：gateway 健康检查已通过 |

## 6. 结论：部署态 GO（拓扑与代码生效路径足以支撑 Task 2/3）

> 本结论仅表示"部署拓扑和代码生效路径足以支撑后续 Sprint 的 Task 2/3"。
> 不表示"本轮已重新验证稳定化代码全部部署生效"。稳定化代码的部署验证见 §4.4 历史支持性事实。

### 与 QUICKSTART 一致性

| QUICKSTART 描述 | 实际观察 | 一致 |
|----------------|---------|------|
| Gateway 8880 | gateway healthy | 是 |
| Job API 8877 | app healthy, Job API binding 8877 | 是 |
| Next.js 3000 | next healthy | 是 |
| Web UI 8876 已废弃 | 未观察到 8876 服务 | 是 |

### 代码生效路径

- app 代码变更通过 **bind mount + docker restart** 生效
- gateway / next 代码变更需要 **docker compose build + up -d**

### 部署态 smoke 路径

当前**没有**不依赖真实外部 API 的自动化部署态 smoke 测试。可用的最近似路径：

- 容器内 `python -c "from core.models import SemanticBlock; ..."` 式的 import 验证
- Job API 健康检查（已由 docker health check 覆盖）
- `tests/test_tts_runtime_evidence.py` 可在本地运行但不在容器内执行

此缺口**不阻塞** rerank readiness review 或 Phase 2，因为这些后续工作的前置条件是代码层 readiness，而非部署态端到端 smoke。
