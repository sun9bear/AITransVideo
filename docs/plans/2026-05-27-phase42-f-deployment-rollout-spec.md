# Phase 4.2 F — Deployment & Canary Rollout Spec

**作者：** Claude Code
**版本：** v0.19（F.0 实测修正：tar exclude 使用 POSIX glob）
**日期：** 2026-05-27
**前置：** E.2 已合并（PR #16，commit `29991ccd`）
**目的：** 把 Phase 4.2 全套代码（A.1/A.2a/A.2b/A.2c/D.1/D.2/E.1/E.2）+ 2 个 migration（030/031）上 US prod，按"admin-only 烟测 → GA flip"两步灰度。

---

## §0 状态对齐

### 0.1 已合并到 main（待部署）

**Phase 4.2 代码（**主链路**全在 main，prod 一行未碰）：**

| 模块 | 路径 | 说明 |
|---|---|---|
| A.1 schema | `gateway/alembic/versions/030_cosyvoice_clone_metadata.py` | **routing / billing / request_id** 元数据：`region_constraint` / `requires_worker` / `target_model` / `worker_provider` / `worker_region` / `clone_api_model` / `billing_sku` / `clone_provider_request_id` / `clone_worker_request_id`。**v0.4 修正**：`source_type` / `clone_sample_seconds` / `clone_sample_segment_ids` 是 **028_user_voice_source_metadata** 已上线的列（A.1 之前的迁移），030 不重复加 |
| A.1 schema | `gateway/alembic/versions/031_user_voice_temp_expiry.py` | `is_temporary` + `temporary_expires_at` + partial index `idx_user_voices_temp_expires_pending` |
| A.2a | `gateway/audio_assembly.py` | ffmpeg 拼接公共抽取（v0.3 修正：无下划线前缀）|
| A.2b | `gateway/cosyvoice_clone/api.py` | `POST /clone` source_segments 主输入 |
| A.2c | `gateway/admin_settings.py`、前端展示层 | GA flag + clone-gate Layer 0.5 |
| D.1 | `gateway/cosyvoice_clone/api.py` | `GET /clone-gate` + 共享授权函数 |
| D.2 | `frontend-next/.../voice-clone/` | `cosyvoiceClone.ts` + `CosyVoiceCloneModal.tsx` + `CosyVoiceConsentModal.tsx` |
| E.1 | `frontend-next/.../VoiceSelectionPanel.tsx`、`VoiceModifyTab.tsx`、`src/pipeline/process.py` 等 | file-upload wiring + A0b `supports_clone` 恢复 |
| E.2 | `frontend-next/.../CosyVoiceSegmentPicker.tsx` 及对应 modal | source_segments 选段 picker UI |

**A0a 已部署到 US prod 的**仅有：sample_uploader fix（commit ≈ A0a 时间点），OSS env vars，武汉 worker .env 指向 WG 内网。其余 Phase 4.2 都 **不在线**。

### 0.2 US prod 现状（A0a 后未变）

- 容器：`aivideotrans-app`（python pipeline + Job API）、`aivideotrans-gateway`、`aivideotrans-next`、`aivideotrans-postgres`、`aivideotrans-caddy`——5 个全部由根 `/opt/aivideotrans/docker-compose.yml` 管理
- 当前 alembic head：**029_pan_backup**（030/031 还没 apply）
- WG tunnel + 武汉 worker：已通烟测；DashScope API key 在**武汉 worker host** 的 env，**不**在 US gateway 的 `.env`（v0.2 修正：v0.1 错误地把它列进 US env）
- OSS：连通自检过；4 项 `AVT_COSYVOICE_OSS_*` env 已就位（v0.2 修正：v0.1 写 4+1 是把 endpoint host 当 region 误算）
- bind mount：`/opt/aivideotrans/config`、`/data/projects`、`/data/jobs`、`/data/runtime_logs`、（dev mode）`src/` / `main.py` / `scripts/`
- 部署脚本：`D:\daili\scripts\Deploy-US-Via-154.cmd`（薄 SCP+SSH 封装）、`SCP-US-Via-154.cmd`、`Invoke-SSH-Via-154-Test-Proxy.ps1`
- target host：`5.78.122.220:22`（via 154 SOCKS5 proxy）

### 0.4 v0.2 sanity 已 grep 确认（防止再凭推断写）

| 事实 | 代码出处 |
|---|---|
| Gateway env prefix `AVT_*` | `gateway/startup_checks.py:178-186`, `gateway/config.py:268`, `gateway/cosyvoice_clone/sample_uploader.py:184-187` |
| OSS env 4 项（**无** REGION）| `ALIYUN_OSS_REQUIRED_SETTINGS` in `sample_uploader.py:183-188` |
| Worker env 4 项 | `startup_checks.py:178-186`：`AVT_MAINLAND_VOICE_WORKER_ENABLED/URL/HMAC_KEY_ID/HMAC_SECRET` |
| Sample uploader env | `AVT_COSYVOICE_SAMPLE_UPLOADER` in `sample_uploader.py:24`、`cosyvoice_clone/api.py:575` |
| Admin settings 字段 | `gateway/admin_settings.py:194-212`：`cosyvoice_clone_worker_enabled` / `cosyvoice_clone_user_allowlist` / `cosyvoice_clone_general_availability_enabled` —— **无** `cosyvoice_clone_admin_only` 字段 |
| DashScope key 在 worker 端 | gateway 仅一处历史 docstring 提到（`gateway/scripts/calibrate_voice_speeds.py:48`），无 runtime 加载——确认在武汉 worker host 的 env |
| Gateway healthz minimal | `gateway/main.py:372-374` 仅返 `{"status": "ok", "auth_required": settings.auth_required}` —— **无** worker/uploader readiness 字段 |
| Worker healthz endpoint | `gateway/mainland_voice_worker.py:135` router prefix = `/api/admin/mainland-voice-worker`；`:169` `router.get("/healthz")` → 实际 path `GET /api/admin/mainland-voice-worker/healthz`（需 admin auth）。v0.1/v0.2 写的 `cosyvoice-worker` 是错的 |
| 030/031 partial index 创建方式 | 都用 `op.create_index(...)`，**非** `CONCURRENTLY` |
| 030 列清单 | `region_constraint` / `requires_worker` / `target_model` / `worker_provider` / `worker_region` / `clone_api_model` / `billing_sku` / `clone_provider_request_id` / `clone_worker_request_id`。**030 不重复加** `source_type` / `clone_sample_seconds` / `clone_sample_segment_ids`——这些在 028_user_voice_source_metadata 已上线（已是 prod schema 的一部分） |
| 028 列（已部署，F.1 不动）| `gateway/alembic/versions/028_user_voice_source_metadata.py:27/41/44` 已经加了 `source_type` / `clone_sample_seconds` / `clone_sample_segment_ids`。Smoke SQL 用 `clone_sample_segment_ids` IS NULL（file 路径）vs 非空（segments 路径）区分；`source_type` 列虽存在但 Phase 4.2 clone endpoint 不一定写它，不用作 smoke 断言 |
| Gateway 容器 import 风格 | `gateway/main.py:442` `from cosyvoice_clone.api import router`；`gateway/cosyvoice_clone/api.py:82-100` 全部 `from cosyvoice_clone.X import Y`。**v0.4 修正**：容器内 python 必须 `from cosyvoice_clone.sample_uploader import ...`，**不能**用 `from gateway.cosyvoice_clone...`（gateway WORKDIR=/opt/gateway 决定的） |
| MainlandWorkerClient factory 模块 | `src/services/mainland_worker/client_factory.py:93` `def build_client_from_env()`，**不在** `client.py`。v0.4 修正：cleanup 在 app 容器内跑（PYTHONPATH 天然含 `src/`），import `from src.services.mainland_worker.client_factory import build_client_from_env` |
| Uploader factory + delete | `gateway/cosyvoice_clone/sample_uploader.py:355` `build_sample_uploader_from_settings(settings)` 是工厂；`:277` `delete_uploaded_url(url)` 是删除接口。**无** `get_uploader()` / `u.healthz()` / `u.delete(key)` |
| Worker delete voice | `src/services/mainland_worker/client.py:348-361` `MainlandWorkerClient.delete_voice(voice_id, WorkerDeleteVoiceRequest(job_id, user_id, reason))`；HMAC 签名 DELETE 到 path `/cosyvoice/voices/{voice_id}`。**无** `/admin/delete_voice` / `X-Worker-Auth` header |
| A0a sample_uploader fix | `sample_uploader.py:259-264` 注释 "不要在 presign 里加 ResponseContentType"；signed_url 不带该 kwarg |
| 临时音色 sweeper | 全仓 grep `temporary_expires_at` 仅命中 schema/ORM/test/spec，**无 runtime sweeper** |
| Compose 含 `next` service | `docker-compose.yml:118-134` `aivideotrans-next:latest` 容器 |
| **没有 `scripts/worker_smoke.py`** | 全仓 grep 只命中 `scripts/pan_backup_smoke.py`；v0.1/v0.2 引用的 worker_smoke 是虚构的 |
| Gateway 容器代码路径 | `gateway/Dockerfile:3` `WORKDIR /opt/gateway`，**不是** `/app/gateway` |
| App 容器代码路径 | 根 `Dockerfile:23` `WORKDIR /opt/aivideotrans/app` |
| DB 用户名 | `docker-compose.yml:106` `POSTGRES_USER: avt`；db 名 `aivideotrans`。`pg_dump -U avt -d aivideotrans` 是正确写法 |
| Admin UI 暴露的 cosyvoice toggle | `frontend-next/src/app/(app)/admin/settings/page.tsx:836-837` 只暴露 `general_availability_enabled` 的 UI checkbox；`worker_enabled` 在 settings type + save body 里**但没暴露 UI 控件**（line 894 仅是 save-through 字段）。**Layer 2 hard kill 需要 CLI 或扩 UI，不能仅靠 admin UI** |

### 0.3 F 部署目标

1. **F.0**：部署前检查（in-flight / DB 状态 / 当前 hash / env 完整性）—— **必须全绿**才进 F.1
2. **F.1**：同步代码到 build context，build 新 gateway image（不 recreate），用 one-off gateway container 跑 031 migration
3. **F.2**：容器 recreate（gateway → app → next.js 顺序）
4. **F.3**：admin-only 初始状态验证（`general_availability_enabled=false`）
5. **F.4**：admin 账号真烟测（含 1 次真 clone + 真 TTS）—— **必须全绿**才进 F.5
6. **F.5**：GA flip（`general_availability_enabled=true`）+ rollback 开关
7. **F.6**：生产监控点 24h 观察
8. **F.7**：明确禁止清单（防止误操作）

---

## §1 范围

### 做什么（F in-scope）

- 一次性部署 Phase 4.2 全套代码 + 030/031 migration 到 US prod（**单**部署窗口）
- 部署后 admin-only 灰度 → admin 真烟测 → GA flip 三步走
- 完整 rollback 剧本：alembic downgrade / 容器镜像 rollback / GA flag 关闭
- 24h 生产监控点

### 不做什么（F out-of-scope）

- ❌ 不改任何代码——F 只是部署 + 灰度，不是 patch 窗口
- ❌ 不上 SG / 其它 region（US 是唯一生产）
- ❌ 不开放任何**新** API 端点——F 用的都是 D.1 / E.1 / E.2 已经在 main 的端点
- ❌ 不动 MiniMax / VolcEngine 旧 clone 路径
- ❌ 不调整 docker-compose（除非 F.0 检查发现 env 不完整必须改）
- ❌ 不开 cosyvoice clone 给非 admin（直到 F.4 烟测全绿才 GA flip）

---

## §2 F.0 部署前检查（无穷小代价，全部必做）

### 2.0.0 命令风格 / 凭证准备约定（v0.8 重写：PowerShell 单引号 + 临时脚本两种格式）

**v0.7 错误（Codex 七审 P1-1）：** v0.7 用 `D:\daili\scripts\SSH-US-Via-154.cmd "DEPLOY_TS=\$(cat ...)"` 这种"反斜杠转义 `$`"的写法。在 PowerShell 里 `\` **不是** `$()` 的转义符（PowerShell 用 backtick `` ` ``），传到 cmd 后 PowerShell 已经把 `\$(...)` 当成字面字符串原样传，再传到远端 bash 时 `\$(...)` 又**阻止**了 bash 命令替换 → `DEPLOY_TS` 在远端会是字面 `$(cat ...)` 字符串而不是文件内容。整个 invariant 不成立。

**v0.8 唯一可执行格式 —— PowerShell 单引号 + `& 调用`：**

```powershell
# 本地 PowerShell：用 & 调用 cmd 脚本，整段 bash 命令包在**单引号**里
# PowerShell 单引号字符串**完全不展开**任何变量 / `$` / 反斜杠
# cmd.exe 透传给 ssh，ssh 透传到远端 bash 解析 → bash 看到原汁原味的 $(cat ...) / ${VAR}
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'DEPLOY_TS=$(cat /opt/aivideotrans/data/runtime_logs/current_phase42_deploy_ts) && DEPLOY_DIR=/opt/aivideotrans/data/runtime_logs/deploy_${DEPLOY_TS} && <实际命令>'
```

**关键约束：**
- **绝对不能**用 PowerShell 双引号包 bash 命令——PowerShell 会就地展开 `${...}` / `$(...)`（PowerShell 自己的变量语法），结果传到远端的不是预期的 bash 命令
- **绝对不能**写 `\$(...)` —— `\` 在 PowerShell 不是转义符，在 bash 里反而阻止命令替换
- **绝对不能**在命令里有**单引号**字符——PowerShell 单引号字符串里的 `'` 必须 `''` 双单引号转义（很容易错）；遇到 bash 命令含单引号 → 走"临时脚本"格式

**复杂命令（含单引号 / heredoc / 多行 python） —— 临时脚本格式：**

```powershell
# 1) 本地编辑器（不是 Set-Content，避免 BOM）创建 D:\Claude\temp\step_NN_<desc>.sh
#    feedback_temp_files.md 约定：临时文件放 D:\Claude\temp\
#    内容：纯 bash，任意复杂度（含单引号 / heredoc / python -c 都 OK）

# 2) SCP 推到远端 /tmp/
& 'D:\daili\scripts\SCP-US-Via-154.cmd' 'D:\Claude\temp\step_NN_<desc>.sh' '/tmp/step_NN_<desc>.sh'

# 3) 远端执行
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'bash /tmp/step_NN_<desc>.sh'

# 4) 本地 + 远端清理
Remove-Item D:\Claude\temp\step_NN_<desc>.sh
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'rm -f /tmp/step_NN_<desc>.sh'
```

**spec 内 `ssh "..."` 字面量含义（v0.8 收紧）：**

- v0.7 之前的 spec 用 `ssh "<cmd>"` 作为简写，正文允许执行时替换
- **v0.8 起所有可执行 spec 命令**已全部改写为 `& 'D:\daili\scripts\SSH-US-Via-154.cmd' '...'` 格式
- **若**仍有遗留 `ssh "..."` 字面量，**伪代码、不可复制执行**——Codex 七审 P1-2 要求消除这种简写以避免照搬

**推文件：** `& 'D:\daili\scripts\Deploy-US-Via-154.cmd' '<local>' '<remote>' '<post-cmd>'` 或 `& 'D:\daili\scripts\SCP-US-Via-154.cmd' '<local>' '<remote>'`。

**绝对禁止：** 手工 PowerShell `ssh` / `scp` 直连 5.78.122.220（绕过 154 SOCKS5 代理）。

**Admin session cookie 准备（§6.x 真烟测多处用到）：**

```
# 浏览器手动获取（推荐，签发后约 7 天有效）：
# 1. Chrome 登录 https://aitrans.video/auth/login（admin: js5559sun@proton.me）
# 2. F12 → Application → Cookies → https://aitrans.video
# 3. **只复制 cookie name=value**，格式：`avt_session=<cookie value string>`
#    ⚠️ 不要复制 DevTools 表格里的 Domain / Path / Expires / SameSite / Secure
#       这些列——`curl -b` 只接受 `name=value` 形式，整行元数据会让 curl 报
#       格式错或把元数据当成多余 cookie 字段发出去
#    范例（正确）：
#      avt_session=eyJhbGciOiJIUzI1NiJ9.xxxxx.yyyy
#    范例（错误，包含元数据）：
#      avt_session  eyJhbG...  aitrans.video  /  Session  ✓  ✓  Lax  ...
# 4. 落到 F.0 §2.0 创建的 deploy log 目录的 admin_session.txt（chmod 600）
#    路径：${DEPLOY_DIR}/admin_session.txt（DEPLOY_DIR 见 §2.0 Step 1）
#    文件内容**就一行**：`avt_session=<cookie value>`（无引号、无换行）
# 5. 后续 curl 用：curl -s -b "$(cat ${DEPLOY_DIR}/admin_session.txt)" <url>
#    -b 期望 `name=value[; name2=value2]` 形式
```

**v0.8 安全写法（PowerShell 本地变量自动读 timestamp，无手抄，cookie 不进 history）：**

```powershell
# 1) 本地用编辑器（不是 Set-Content / echo）建 D:\Claude\temp\admin_session.txt
#    内容**就一行**：avt_session=<cookie value>
#    （feedback_temp_files.md：临时文件放 D:\Claude\temp\，不要桌面）

# 2) PowerShell 自动读远端持久 timestamp 到本地变量
$DEPLOY_TS = (& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'cat /opt/aivideotrans/data/runtime_logs/current_phase42_deploy_ts').Trim()
$DEPLOY_DIR = "/opt/aivideotrans/data/runtime_logs/deploy_${DEPLOY_TS}"
Write-Host "DEPLOY_DIR = $DEPLOY_DIR"

# 3) SCP 推（PowerShell 双引号在 SCP **目标路径**里允许展开 $DEPLOY_DIR——不是 bash 命令体）
& 'D:\daili\scripts\SCP-US-Via-154.cmd' 'D:\Claude\temp\admin_session.txt' "${DEPLOY_DIR}/admin_session.txt"

# 4) 远端 chmod —— v0.10 修正（Codex 九审 P1-1）：与"所有远端 bash 命令读持久 DEPLOY_TS"规则
#    保持一致；改用单引号 + 远端 cat 读，**不**依赖本地 $DEPLOY_DIR 展开
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'DEPLOY_TS=$(cat /opt/aivideotrans/data/runtime_logs/current_phase42_deploy_ts) && DEPLOY_DIR=/opt/aivideotrans/data/runtime_logs/deploy_${DEPLOY_TS} && chmod 600 ${DEPLOY_DIR}/admin_session.txt'

# 5) 立即清理本地副本（cookie 落本地磁盘不安全，仅传输用）
Remove-Item D:\Claude\temp\admin_session.txt
```

**v0.9 错误纠正（Codex 九审 P1-1）：** v0.9 步骤 4 用 PowerShell 双引号 `"chmod 600 ${DEPLOY_DIR}/admin_session.txt"` 依赖本地 `$DEPLOY_DIR` 展开。这破坏了 v0.7 之后"所有远端 bash 命令一律读远端持久 `DEPLOY_TS`"的统一规则——执行者会困惑哪个 step 该走哪条路径。**v0.10 统一**：远端 bash 命令体（含路径 / chmod / 任何文件操作）一律走 §2.0 Step 2 模板（单引号 + 远端 cat）；SCP 的**目标路径参数**（不是 bash 命令）仍允许 PowerShell 双引号展开本地 `$DEPLOY_DIR`，因为 SCP 是 cmd 级参数不进 bash。

cookie 不能 commit / 不能进 audit log / 不能进 SSH 命令 history；F.6 结束时显式删除 `${DEPLOY_DIR}/admin_session.txt`（远端）。

### 2.0 创建本次部署的 log 目录 + 持久化 `DEPLOY_TS` / `DEPLOY_DIR`（v0.7 重写：跨 SSH 持久化）

**v0.6 错误（Codex 六审 P1）：** v0.6 在本地 shell 写 `TS=$(date ...)` + 后续 `ssh "... deploy_${TS} ..."`，期待每个 `SSH-US-Via-154.cmd "..."` 仍能看到 `$TS`。但每次 SSH-via-154 都是独立的远端 shell 子进程，`$TS` 在远端**不存在**。结果：要么 path 解析成 `deploy_/...`（空 $TS），要么解析成本地 shell 的 cwd 副本（PowerShell + bash 混用更糟）。

**v0.7 正确做法：** 把 timestamp 写到远端持久文件，所有后续远端命令一律开头 `DEPLOY_TS=$(cat ...)` 读出来再用。

**Step 1：创建本次部署的 timestamp + 目录（**远端**唯一来源；v0.8 PowerShell 单引号格式）：**

```powershell
# 本地 PowerShell —— 整段 bash 在单引号里，PowerShell 不展开任何东西
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'DEPLOY_TS=$(date +%Y%m%d_%H%M%S) && echo "${DEPLOY_TS}" > /opt/aivideotrans/data/runtime_logs/current_phase42_deploy_ts && mkdir -p /opt/aivideotrans/data/runtime_logs/deploy_${DEPLOY_TS} && echo "DEPLOY_DIR=/opt/aivideotrans/data/runtime_logs/deploy_${DEPLOY_TS}"'
```

**Step 2：后续所有远端命令的标准前缀（**v0.8 约定**）：**

每条 `& 'D:\daili\scripts\SSH-US-Via-154.cmd' '...'` 的 bash 命令以下面这一行开头（**单行 inline**，避免 PowerShell 引号嵌套）：

```bash
DEPLOY_TS=$(cat /opt/aivideotrans/data/runtime_logs/current_phase42_deploy_ts) && DEPLOY_DIR=/opt/aivideotrans/data/runtime_logs/deploy_${DEPLOY_TS} && <step 命令>
```

PowerShell 调用整段范例：

```powershell
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'DEPLOY_TS=$(cat /opt/aivideotrans/data/runtime_logs/current_phase42_deploy_ts) && DEPLOY_DIR=/opt/aivideotrans/data/runtime_logs/deploy_${DEPLOY_TS} && <step command using ${DEPLOY_DIR}>'
```

**Step 3：F.6 结束之后清理 timestamp 标记**（防止下次部署误读旧 ts）：

```powershell
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'mv /opt/aivideotrans/data/runtime_logs/current_phase42_deploy_ts /opt/aivideotrans/data/runtime_logs/deploy_$(cat /opt/aivideotrans/data/runtime_logs/current_phase42_deploy_ts)/_deploy_ts.txt'
```

**通过标准：**
- `current_phase42_deploy_ts` 文件存在且**只含一行** `YYYYMMDD_HHMMSS`
- `deploy_${DEPLOY_TS}/` 目录创建成功
- 后续步骤的每条 SSH 命令开头**都能**读到正确的 `$DEPLOY_TS` / `$DEPLOY_DIR`

**spec 内 `${TS}` 字面量统一含义：** 全文 v0.7 起把 `${TS}` 视为 `${DEPLOY_TS}` 的同义记法（spec 阅读时按 v0.7 §2.0 Step 2 模式展开成"先 cat 再用"，不要直接当 shell 变量）。

### 2.1 In-flight pipeline psql 检查（**最重要**）

**为什么必做：** `feedback_compose_env_file_recreate.md` 教训——`docker compose ... up -d <service>` 会触发整 project 重新插值 → 依赖 service 顺带 recreate。F.2 一定会动 gateway 或 app；任何在跑的 pipeline 都会被中断，已扣的额度不退。

**操作：**

```sql
-- 1. 查所有 active job
SELECT id, user_id, status, current_stage, updated_at, created_at
FROM jobs
WHERE status IN ('pending', 'running', 'editing', 'paused', 'awaiting_review')
ORDER BY updated_at DESC
LIMIT 50;

-- 2. 查 worker_active 状态的 job（pipeline 在跑）
SELECT id, status, current_stage, started_at
FROM jobs
WHERE status IN ('running')
  AND current_stage IS NOT NULL
ORDER BY started_at DESC;

-- 3. 查最近 30 分钟有日志写入的 job（间接判断活跃度）
SELECT job_id, MAX(created_at) as last_event
FROM job_events
WHERE created_at > now() - interval '30 minutes'
GROUP BY job_id
ORDER BY last_event DESC;
```

**通过标准（v0.2 收紧分级，Codex P2）：**

- **Blocker**（任一非空 → 不部署）：`status IN ('running', 'pending', 'awaiting_review')`
- **Awareness**（提示但不阻断）：`status IN ('editing', 'paused')`
  - `editing`：用户在 Studio 编辑中，draft 落本地 + editing/cancel 路径，重启不会丢；但 deploy 后用户**可能**因 session 触发 re-fetch 看到加载状态
  - `paused`：用户主动暂停的任务，重启后状态保持

**查询拆分（避免 v0.1 把 editing 误算成 blocker）：**

```sql
-- Blocker check (must be empty)
SELECT id, user_id, status, current_stage, updated_at
FROM jobs
WHERE status IN ('running', 'pending', 'awaiting_review')
ORDER BY updated_at DESC
LIMIT 50;

-- Awareness only (record count, OK to proceed)
SELECT status, count(*) FROM jobs
WHERE status IN ('editing', 'paused')
GROUP BY status;
```

### 2.2 当前 DB alembic head

```bash
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'cd /opt/aivideotrans && docker compose --env-file /opt/aivideotrans/config/.env exec -T gateway alembic current'
# 期望输出：029_pan_backup (head)
```

通过标准：head 是 **030_cosyvoice_clone_metadata**。F.0 实测生产当前就是 030：Phase 4.1 的 clone metadata schema 已经部署；F 只需要补 031 `user_voices` temporary expiry migration。若已经是 031 → 说明有人提前部署过，停下来调查 schema/data；若仍是 029 → 说明 030 未部署，必须回到 Phase 4.1 migration 复核。

### 2.3 当前各 container 镜像 hash + 版本

```bash
# v0.3：5 个 compose-managed 容器都查
for svc in aivideotrans-gateway aivideotrans-app aivideotrans-next aivideotrans-postgres aivideotrans-caddy; do
  echo "=== $svc ==="
  docker inspect "$svc" --format '{{.Image}} {{.Config.Labels.git_commit}} {{.State.Status}}'
done

# Next 走 compose service（v0.3 修正：v0.2 写的 next-standalone BUILD_ID 是手工铺路径，与
# 实际 compose `next` service 不一致）
docker inspect aivideotrans-next --format 'BuildLabel: {{index .Config.Labels "phase42.build" }}, Status: {{.State.Status}}, Health: {{if .State.Health}}{{.State.Health.Status}}{{else}}n/a{{end}}'
```

通过标准：记录到 `runtime_logs/deploy_<timestamp>/pre_state.txt`，rollback 时用得上。

### 2.4 env 完整性 audit（v0.2 修正：env 名 + 文件位置）

**Gateway 用的 env 在 `/opt/aivideotrans/config/.env`**（compose 通过 `--env-file` 加载，**不是** project 根 `.env`）。生产 compose 调用必须显式 `--env-file /opt/aivideotrans/config/.env`，否则会从 cwd `.env` 退化导致变量缺失（feedback_compose_env_file_recreate.md 教训）。

| 变量 | 用途 | 失败影响 |
|---|---|---|
| `AVT_INTERNAL_API_KEY` | gateway → Job API 内部认证 | gateway 启动 critical fail（`startup_checks.validate_internal_api_key`）|
| `AVT_MAINLAND_VOICE_WORKER_ENABLED` | worker 路径总开关 | 关 → clone-gate `runtime_ready=false`，`runtime_unavailable_code="worker_disabled"` |
| `AVT_MAINLAND_VOICE_WORKER_URL` | 武汉 worker 内网 URL | `startup_checks.py:178` critical |
| `AVT_MAINLAND_VOICE_WORKER_HMAC_KEY_ID` | worker HMAC key id | `startup_checks.py:180` critical |
| `AVT_MAINLAND_VOICE_WORKER_HMAC_SECRET` | worker HMAC secret | `startup_checks.py:182` critical |
| `AVT_COSYVOICE_SAMPLE_UPLOADER` | uploader 实现选择 | 非 `aliyun_oss` → clone-gate `runtime_unavailable_code="sample_uploader_not_implemented"` |
| `AVT_COSYVOICE_OSS_ENDPOINT` | OSS endpoint（含 region 信息，如 `oss-cn-shanghai.aliyuncs.com`）| `sample_uploader_config_missing` |
| `AVT_COSYVOICE_OSS_BUCKET` | OSS bucket | 同上 |
| `AVT_COSYVOICE_OSS_ACCESS_KEY_ID` | OSS AK | 同上 |
| `AVT_COSYVOICE_OSS_ACCESS_KEY_SECRET` | OSS SK | 同上 |
| `R2_*`（5 项）| Phase 2 下载后端 | 已有，与 F 无关，不改 |

**不在 US gateway env 的项：**
- ❌ `DASHSCOPE_API_KEY` —— 在**武汉 worker host** 的 env，**不**在 US。F 部署不动 worker，所以不查这个
- ❌ 不存在 `AVT_COSYVOICE_OSS_REGION` —— `_ENDPOINT` 已带 region 信息

**v0.9 改为临时脚本格式（heredoc + 单引号嵌套地狱，inline 不可执行）：**

**Step 1**：本地编辑器（不是 `Set-Content` —— 避免 BOM）创建 `D:\Claude\temp\f_step24_env_audit.py`，内容：

```python
# f_step24_env_audit.py — F.0 §2.4 env 完整性校验（pure python，无 heredoc）
import sys

REQUIRED = [
    "AVT_INTERNAL_API_KEY",
    "AVT_MAINLAND_VOICE_WORKER_ENABLED",
    "AVT_MAINLAND_VOICE_WORKER_URL",
    "AVT_MAINLAND_VOICE_WORKER_HMAC_KEY_ID",
    "AVT_MAINLAND_VOICE_WORKER_HMAC_SECRET",
    "AVT_COSYVOICE_SAMPLE_UPLOADER",
    "AVT_COSYVOICE_OSS_ENDPOINT",
    "AVT_COSYVOICE_OSS_BUCKET",
    "AVT_COSYVOICE_OSS_ACCESS_KEY_ID",
    "AVT_COSYVOICE_OSS_ACCESS_KEY_SECRET",
]
EXPECT_EQ = {
    "AVT_MAINLAND_VOICE_WORKER_ENABLED": "true",
    "AVT_COSYVOICE_SAMPLE_UPLOADER": "aliyun_oss",
}

found = {}
with open("/opt/aivideotrans/config/.env", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key in REQUIRED:
            found[key] = val

missing = [k for k in REQUIRED if k not in found]
empty = [k for k in REQUIRED if k in found and not found[k]]
wrong = [(k, found.get(k, ""), EXPECT_EQ[k])
         for k in EXPECT_EQ if found.get(k, "").lower() != EXPECT_EQ[k]]

print("missing:", missing)
print("empty:", empty)
print("wrong_value:", wrong)
if missing or empty or wrong:
    sys.exit(1)
print("OK: all 10 required AVT_* present, non-empty, and key values match expectations")
```

**Step 2**：本地 PowerShell：

```powershell
& 'D:\daili\scripts\SCP-US-Via-154.cmd' 'D:\Claude\temp\f_step24_env_audit.py' '/tmp/f_step24_env_audit.py'
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'python3 /tmp/f_step24_env_audit.py'
# 清理
Remove-Item D:\Claude\temp\f_step24_env_audit.py
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'rm -f /tmp/f_step24_env_audit.py'
```

**通过标准：** SSH-US-Via-154.cmd 退出码 0 + 打印 'OK: all 10 required ...'。任一 missing / empty / wrong → 停止，先补 env 再部署。

**为什么不用 `grep | wc -l`：** `grep '^AVT_X='` 会匹配 `AVT_X=`（空值），让审核以为已配置但实际启动会因为空值 fail（startup_checks.py 对 `worker_url=""` / `hmac_secret=""` 会 critical）。v0.3 这条审计形同虚设。

### 2.4b app 容器 import sanity（v0.15：收口 §16 第 3 项）

**目的：** F.6 cleanup 的 worker delete 脚本在 `aivideotrans-app` 容器内 import
`from src.services.mainland_worker.client_factory import build_client_from_env`。F.0 阶段必须先证明 app 容器的
`WORKDIR=/opt/aivideotrans/app` + `PYTHONPATH=/opt/aivideotrans/app/src` 能正确导入该模块，并且 env 足够让 factory 返回 client。

**本地编辑器创建** `D:\Claude\temp\f_step24b_app_import_sanity.sh`：

```bash
#!/bin/bash
# f_step24b_app_import_sanity.sh — F.0 app 容器 import + worker client factory sanity
set -euo pipefail

docker exec -i aivideotrans-app python3 <<'PYEOF'
from src.services.mainland_worker.client_factory import build_client_from_env

client = build_client_from_env()
print({
    "client_factory_import_ok": True,
    "client_configured": client is not None,
})
if client is None:
    raise SystemExit("build_client_from_env returned None; rerun §2.4 env audit before deploy")
PYEOF
```

```powershell
& 'D:\daili\scripts\SCP-US-Via-154.cmd' 'D:\Claude\temp\f_step24b_app_import_sanity.sh' '/tmp/f_step24b_app_import_sanity.sh'
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'bash /tmp/f_step24b_app_import_sanity.sh'
Remove-Item D:\Claude\temp\f_step24b_app_import_sanity.sh
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'rm -f /tmp/f_step24b_app_import_sanity.sh'
```

**通过标准：** 输出 `client_factory_import_ok=true` 且 `client_configured=true`。失败则停止 F，不进入 F.1。

### 2.5 备份（v0.7 修正：用持久 DEPLOY_TS 而不是临时 TS）

**v0.8 推荐走临时脚本（命令含 `tar --exclude='...'` 等单引号，inline PowerShell 单引号会嵌套地狱）：**

```powershell
# 1) 本地编辑器（不是 Set-Content / echo —— 避免 BOM）创建：
#    D:\Claude\temp\f_step25_backup.sh
#    内容（pure bash，任意单引号 / 反斜杠都 OK）：
```

```bash
#!/bin/bash
# f_step25_backup.sh — F.0 §2.5 四件套备份
set -euo pipefail

DEPLOY_TS=$(cat /opt/aivideotrans/data/runtime_logs/current_phase42_deploy_ts)
DEPLOY_DIR=/opt/aivideotrans/data/runtime_logs/deploy_${DEPLOY_TS}

mkdir -p /opt/aivideotrans/backups

docker exec aivideotrans-postgres pg_dump -U avt -d aivideotrans \
  | gzip > /opt/aivideotrans/backups/db_pre_phase42_${DEPLOY_TS}.sql.gz

cp /opt/aivideotrans/config/admin_settings.json \
   /opt/aivideotrans/backups/admin_settings_${DEPLOY_TS}.json

cp /opt/aivideotrans/docker-compose.yml \
   /opt/aivideotrans/backups/docker-compose_${DEPLOY_TS}.yml

cd /opt/aivideotrans && tar \
  --exclude='./data' --exclude='./backups' \
  --exclude='./staging' \
  --exclude='*/node_modules' --exclude='*/node_modules/*' \
  --exclude='*/.next' --exclude='*/.next/*' \
  --exclude='*/.pytest_cache' --exclude='*/.pytest_cache/*' \
  --exclude='*/__pycache__' --exclude='*/__pycache__/*' \
  --exclude='./.git' \
  -czf /opt/aivideotrans/backups/app_pre_phase42_${DEPLOY_TS}.tar.gz ./app

ls -lh /opt/aivideotrans/backups/*${DEPLOY_TS}* > ${DEPLOY_DIR}/backup_inventory.txt
cat ${DEPLOY_DIR}/backup_inventory.txt
```

```powershell
# 2) SCP 推上去
& 'D:\daili\scripts\SCP-US-Via-154.cmd' 'D:\Claude\temp\f_step25_backup.sh' '/tmp/f_step25_backup.sh'

# 3) 远端执行
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'bash /tmp/f_step25_backup.sh'

# 4) 清理
Remove-Item D:\Claude\temp\f_step25_backup.sh
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'rm -f /tmp/f_step25_backup.sh'
```

通过标准：
- 4 个备份产物（db.sql.gz / admin_settings.json / docker-compose.yml / app.tar.gz）全部落到 `/opt/aivideotrans/backups/`
- `${DEPLOY_DIR}/backup_inventory.txt` 含全部 4 行
- app.tar.gz 不包含 node_modules / `.next` / pytest cache / pycache / staging 等可重建或临时目录
- 磁盘有 ≥ 10 GB 剩余（`df -h /opt/aivideotrans` 复核）

**v0.6 错误纠正（Codex 六审 P1）：** v0.6 写 `TS=$(date +%Y%m%d_%H%M%S)` 然后后续 `${TS}` 是**单个 bash snippet** —— 如果用户分行复制到多个 SSH-via-154 命令，每次远端 shell 重新启动，`TS` 空。v0.7 改用持久文件 `current_phase42_deploy_ts`，单次 SSH 调用内连续 `&&` 也可，跨 SSH 调用就必须先 `cat` 读出。

### 2.6 user_voices 行数 precheck（migration 锁评估，§3.1 引用）

```sql
SELECT count(*) AS user_voices_rows,
       count(*) FILTER (WHERE provider = 'cosyvoice_voice_clone') AS cosyvoice_clone_rows
FROM user_voices;
```

通过标准：`user_voices_rows < 10_000` → §3.1 一档（直接 upgrade）。10k-100k → 二档（凌晨窗口）。>100k → 改 migration 用 CONCURRENTLY。

---

## §3 F.1 Migration 执行（v0.19：生产当前 030，只补 031）

### 3.1 030 + 031 都是普通 `op.create_index`（**非 CONCURRENTLY**）—— Codex 已确认

**事实**：grep 确认 030/031 均用 `op.create_index(...)`，会**短锁** `user_voices` 表。Postgres `CREATE INDEX`（非 concurrently）的锁是 `SHARE` 锁：阻塞写、不阻塞读。锁时长 ∝ 表行数。

**v0.2 加 row-count precheck（Codex P1 #3）：**

```sql
-- 阈值决策（在 F.0 阶段跑）
SELECT count(*) AS user_voices_rows FROM user_voices;
```

| 行数 | 判断 | 处置 |
|---|---|---|
| `< 10_000` | 短锁 < 1 秒，可接受 | 按 §3.2 一次性 upgrade |
| `10_000 – 100_000` | 锁 1-10 秒，业务低峰期可接受 | 部署窗口选凌晨 + 在 in-flight precheck 通过后操作；若有写入冲突，alembic 会自动 retry 一次 |
| `> 100_000` | 锁可能 > 10 秒，影响 voice library 写 | **停止 F.1**，改 migration 用 `CREATE INDEX CONCURRENTLY`（需要拆出来 op.execute），或分两个部署窗口 |

US prod 当前 user_voices 行数预期在 **几百 ~ 几千**（Phase 4.1 上线后累积），属于 <10k 区间。F.0 必须实测一遍。

### 3.2 031 upgrade 流程（先同步代码 / 构建 gateway image，但不 recreate）

**F.0 实测事实：** US prod 当前 gateway build context 与运行中 gateway image 都只到 `030_cosyvoice_clone_metadata`；本地 main 才有 `031_user_voice_temp_expiry.py`。因此不能在旧 gateway container 内直接 `alembic upgrade head`，否则旧 image 的 `head` 仍是 030。正确顺序：

1. 先执行 §4.3 代码同步，把 main HEAD 发布到 `/opt/aivideotrans/app` build context。
2. 只执行 `docker compose --env-file /opt/aivideotrans/config/.env build gateway`，**不** `up -d gateway`，不替换运行中 gateway。
3. 用新 gateway image 开 one-off container 跑 `alembic upgrade head`，此时 head 才是 031。
4. 031 成功后，再进入 §4.4/§4.5/§4.6 recreate gateway/app/next。

**Review checklist（pre-flight）：**
- [ ] §3.1 行数 precheck 落在 <10k（或与处置规则一致）
- [ ] dry-run SQL 全部是 `ALTER TABLE ... ADD COLUMN` 或 `CREATE INDEX`，无 `DROP` / `ALTER COLUMN TYPE`
- [ ] 030 不依赖未上线的应用代码
- [ ] 031 的 partial index 的 WHERE 子句正确：`WHERE is_temporary = TRUE AND expired_at IS NULL`

**Dry-run + 实际 upgrade（v0.13 修正：脚本化，禁止裸 `docker compose` 复制到本地执行）：**

```bash
#!/bin/bash
# f_step32_migration.sh — F.1 migration dry-run + upgrade
set -euo pipefail

cd /opt/aivideotrans

echo "=== alembic current (before) ==="
docker compose --env-file /opt/aivideotrans/config/.env exec -T gateway alembic current

echo "=== build gateway image with new migration files (no recreate) ==="
docker compose --env-file /opt/aivideotrans/config/.env build gateway

echo "=== dry-run SQL 030 -> 031 ==="
docker compose --env-file /opt/aivideotrans/config/.env run --rm --no-deps gateway \
  alembic upgrade --sql 030_cosyvoice_clone_metadata:031_user_voice_temp_expiry > /tmp/migration_031.sql

if grep -Eiq 'DROP TABLE|DROP COLUMN|ALTER COLUMN .* TYPE' /tmp/migration_031.sql; then
    echo "ERROR: migration dry-run contains destructive SQL; stop before upgrade"
    grep -Ein 'DROP TABLE|DROP COLUMN|ALTER COLUMN .* TYPE' /tmp/migration_031.sql
    exit 1
fi

tee /opt/aivideotrans/data/runtime_logs/deploy_$(cat /opt/aivideotrans/data/runtime_logs/current_phase42_deploy_ts)/migration_031.sql < /tmp/migration_031.sql >/dev/null

echo "=== applying alembic head ==="
docker compose --env-file /opt/aivideotrans/config/.env run --rm --no-deps gateway alembic upgrade head

echo "=== alembic current (after) ==="
docker compose --env-file /opt/aivideotrans/config/.env run --rm --no-deps gateway alembic current
# 期望输出含：031_user_voice_temp_expiry (head)
```

```powershell
& 'D:\daili\scripts\SCP-US-Via-154.cmd' 'D:\Claude\temp\f_step32_migration.sh' '/tmp/f_step32_migration.sh'
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'bash /tmp/f_step32_migration.sh'
Remove-Item D:\Claude\temp\f_step32_migration.sh
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'rm -f /tmp/f_step32_migration.sh'
```

**期望耗时：** < 5 秒（行数 <10k）。**超过 30 秒 → 异常**，立即 ctrl+c 并查 lock 等待状态。

### 3.3 Rollback 路径（**v0.2 收紧：写入后原则上不 downgrade**）

**核心原则（Codex P1 #3）：**

- **未进流量阶段（F.1 刚结束 / F.2 中 / F.4 烟测前）**：可以 downgrade，schema 干净，无数据丢失风险
- **F.4 真 clone 之后**：user_voices 表已经有 `is_temporary` / `temporary_expires_at` / `clone_sample_seconds` 等列的非默认值
  - **原则上不 downgrade**——丢用户付费产生的元数据
  - 优先用 **Layer 1 / Layer 2 flag rollback**（§7.3）+ **Layer 3 镜像 rollback**
  - 仅在"schema 本身被证明有 data corruption / index 死锁"等极端 case 才考虑 downgrade，且**必须先 DB snapshot**

**操作命令（仅适用未进流量场景；v0.13 自审修正：rollback 也必须脚本化）：**

```bash
#!/bin/bash
# f_rollback_schema_pretraffic.sh — 仅限 F.4 真 clone 前的 schema rollback
set -euo pipefail

cd /opt/aivideotrans

echo "=== current before schema rollback ==="
docker compose --env-file /opt/aivideotrans/config/.env exec -T gateway alembic current

if [[ "${ROLLBACK_TARGET:-029}" == "030" ]]; then
    echo "=== downgrade 031 -> 030 only ==="
    docker compose --env-file /opt/aivideotrans/config/.env exec -T gateway alembic downgrade 030_cosyvoice_clone_metadata
else
    echo "=== downgrade 031/030 -> 029 ==="
    docker compose --env-file /opt/aivideotrans/config/.env exec -T gateway alembic downgrade 029_pan_backup
fi

echo "=== current after schema rollback ==="
docker compose --env-file /opt/aivideotrans/config/.env exec -T gateway alembic current
```

```powershell
& 'D:\daili\scripts\SCP-US-Via-154.cmd' 'D:\Claude\temp\f_rollback_schema_pretraffic.sh' '/tmp/f_rollback_schema_pretraffic.sh'
# 默认回滚到 029。若只退 031 -> 030，把下一行 bash 前加：ROLLBACK_TARGET=030
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'bash /tmp/f_rollback_schema_pretraffic.sh'
Remove-Item D:\Claude\temp\f_rollback_schema_pretraffic.sh
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'rm -f /tmp/f_rollback_schema_pretraffic.sh'
```

**Code vs schema 兼容性矩阵：**

| Schema | Code | 状态 |
|---|---|---|
| 029 | 029（旧）| ✅ 当前 prod 状态 |
| 031 | 029（旧）| ✅ 新列被忽略，向后兼容 |
| 031 | Phase 4.2 | ✅ 目标状态 |
| 029 | Phase 4.2 | ❌ 启动 fail（ORM 找不到列）|

→ **部署流程严格 schema 先，code 后；rollback 严格 code 先，schema 后**。F.1 → F.2 顺序不能反。

---

## §4 F.2 代码部署顺序

### 4.1 顺序：**gateway → app → next.js**

**为什么这个顺序：**

1. **Gateway 先**：030/031 schema 已经在 F.1 apply；gateway 的 ORM 反射 + clone-gate / clone endpoint 是新 schema 的唯一消费者。先让 gateway 跑起来确认 ORM 兼容。
2. **App 次之**：pipeline 进程通过 `requires_worker` / `worker_target_model` 路由字段消费 gateway 的输出；gateway 必须先准备好这些字段。
3. **Next.js 最后**：前端是用户入口，最后切才不会让用户看到"按钮亮起但后端 404"。

**反之的 risk：**
- next.js 先切：用户点 clone 按钮 → 后端 gateway 是旧版 → 404 / 500
- app 先切：pipeline 跑到一半发现 gateway 没回 routing field → fail-closed（已有守卫）但白白 burn 一次任务

### 4.2 通用命令模板（**v0.2 锁定**：根 compose + `--env-file`）

**生产规范** —— 所有 docker compose 操作必须从 `/opt/aivideotrans` 根目录执行，且**必须**显式 `--env-file /opt/aivideotrans/config/.env`，否则会从 cwd 退化、变量缺失（feedback_compose_env_file_recreate.md 教训）：

```bash
cd /opt/aivideotrans
docker compose --env-file /opt/aivideotrans/config/.env <cmd>
```

不允许在 spec 命令里出现：
- ❌ `docker compose build <svc>`（无 `--env-file`，会从 cwd 退化）
- ❌ `cd /opt/aivideotrans/gateway && ...` 手工换目录
- ❌ `mv gateway gateway.bak`（这种 atomic dir swap 不是 compose 友好的部署模型）
- ❌ 手工铺 `.next/standalone` 到 host 路径（绕过 `next` service build context）

### 4.3 代码同步（一次同步整个 `/opt/aivideotrans/app` 工作树）

**Phase 4.2 改了 gateway/、src/、frontend-next/、tests/、docs/、`docker-compose.yml`** —— 一次性同步整树，避免分文件 SCP 漏文件。

```powershell
# 1. 本地打包整个工作树（git archive 保留 mode，排除 .git）
Set-Location 'D:\Claude\AIVideoTrans_Codex_web_mvp'
$HEAD = (git rev-parse HEAD).Trim()
Set-Content -Path 'D:\Claude\temp\.git_archive_commit' -Value $HEAD -NoNewline -Encoding ascii
git archive --format=tar.gz --add-file='D:\Claude\temp\.git_archive_commit' -o 'D:\Claude\temp\release_phase42.tar.gz' HEAD

# 2. 推送 + 解到 staging 目录
& 'D:\daili\scripts\SCP-US-Via-154.cmd' 'D:\Claude\temp\release_phase42.tar.gz' '/tmp/release_phase42.tar.gz'
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'rm -rf /opt/aivideotrans/staging && mkdir -p /opt/aivideotrans/staging && tar xzf /tmp/release_phase42.tar.gz -C /opt/aivideotrans/staging'
Remove-Item D:\Claude\temp\release_phase42.tar.gz
Remove-Item D:\Claude\temp\.git_archive_commit

# 3-4. 同步到 app/ + 同步根 docker-compose.yml —— v0.10 改临时脚本（见下方 `f_step43_sync.sh`）
```

**v0.10 同步 + 校验合并临时脚本 `f_step43_sync.sh`**：

```bash
#!/bin/bash
# F.2 §4.3 — staging → app 树同步 + docker-compose.yml + 关键文件 sha256 cross-host 校验
set -euo pipefail

cd /opt/aivideotrans

# 1) rsync 排除 .env / admin_settings.json / data/ / backups/ / .git / node_modules / __pycache__
rsync -a --delete \
  --exclude='.env' \
  --exclude='admin_settings.json' \
  --exclude='data/' \
  --exclude='backups/' \
  --exclude='.git' \
  --exclude='node_modules/' \
  --exclude='__pycache__/' \
  staging/ app/

# 2) 同步根 docker-compose.yml
cp /opt/aivideotrans/staging/docker-compose.yml /opt/aivideotrans/docker-compose.yml

# 3) 关键文件 sha256 cross-host 校验：app/ vs staging/ 一致
for f in gateway/main.py \
         gateway/cosyvoice_clone/api.py \
         gateway/cosyvoice_clone/sample_uploader.py \
         frontend-next/src/components/voice-clone/CosyVoiceSegmentPicker.tsx \
         frontend-next/src/components/voice-clone/CosyVoiceCloneModal.tsx \
         docker-compose.yml; do
    if [[ -f "app/$f" && -f "staging/$f" ]]; then
        sha256sum "app/$f" "staging/$f"
        a=$(sha256sum "app/$f" | awk '{print $1}')
        b=$(sha256sum "staging/$f" | awk '{print $1}')
        if [[ "$a" != "$b" ]]; then
            echo "ERROR: $f sha256 mismatch (app vs staging) —— rsync 未生效"
            exit 1
        fi
    fi
done

# 4) staging 树 git commit id（git archive 自带或单独 SCP 上来的 .git_archive_commit）
echo "=== staging git commit id ==="
if [[ -f "/opt/aivideotrans/staging/.git_archive_commit" ]]; then
    cat /opt/aivideotrans/staging/.git_archive_commit
else
    echo "ERROR: no .git_archive_commit found — release archive must be built with git archive --add-file=.git_archive_commit"
    exit 1
fi

echo
echo "OK: app/ tree synced from staging/, 6 key files sha256 match"
```

```powershell
& 'D:\daili\scripts\SCP-US-Via-154.cmd' 'D:\Claude\temp\f_step43_sync.sh' '/tmp/f_step43_sync.sh'
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'bash /tmp/f_step43_sync.sh'
Remove-Item D:\Claude\temp\f_step43_sync.sh
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'rm -f /tmp/f_step43_sync.sh'
```

**期望：** 退出码 0；打印 "OK: app/ tree synced from staging/, 6 key files sha256 match" + staging git commit id 与本地 push 的 HEAD 一致。

注：`git archive --format=tar.gz` 不包含 `.git/`，所以 staging 树不是 git repo。v0.13 已固定为：本地用 PowerShell `Set-Content -Encoding ascii -NoNewline` 生成 `D:\Claude\temp\.git_archive_commit`，再用 `git archive --add-file='D:\Claude\temp\.git_archive_commit' ... HEAD` 把 commit marker 放进 archive root，`f_step43_sync.sh` 必须能读到 `/opt/aivideotrans/staging/.git_archive_commit`。

### 4.4 Gateway rebuild + recreate

```bash
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'cd /opt/aivideotrans &&  docker compose --env-file /opt/aivideotrans/config/.env build gateway &&  docker compose --env-file /opt/aivideotrans/config/.env up -d --no-deps --force-recreate gateway'

# 等待 minimal healthz（v0.3 修正：endpoint 只返基础 ok + auth_required，**不含**
# worker/uploader 状态——具体的 worker / uploader 探活在 F.4 烟测里做）
# v0.10 修正（Codex 九审 P1-3）：healthcheck loop 改单行；反斜杠跨行在文档复制时易丢
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'for i in 1 2 3 4 5 6; do curl -s http://localhost:8880/gateway/health | jq . && break || sleep 5; done'
# 期望 200：{"status": "ok", "auth_required": true|false}
```

**Worker / uploader 探活分别另外做（v0.3 修正：v0.2 错把这些字段挂在 `/gateway/health` 上）**：

**Worker probe 临时脚本 `f_step44_worker_probe.sh`**（v0.9 改临时脚本 + 读 cookie 文件）：

```bash
#!/bin/bash
# F.2 §4.4 — gateway rebuild 后 admin worker healthz probe
set -euo pipefail

DEPLOY_TS=$(cat /opt/aivideotrans/data/runtime_logs/current_phase42_deploy_ts)
DEPLOY_DIR=/opt/aivideotrans/data/runtime_logs/deploy_${DEPLOY_TS}

curl -s -b "$(cat ${DEPLOY_DIR}/admin_session.txt)" \
     http://localhost:8880/api/admin/mainland-voice-worker/healthz \
  | jq .
# 期望 200：{"ok": true, "worker": "...", "region": "...", "providers": [...]}
# v0.1/v0.2 写的 `/api/admin/voice/cosyvoice-worker/healthz` 是错的，实际是 mainland-voice-worker
```

```powershell
& 'D:\daili\scripts\SCP-US-Via-154.cmd' 'D:\Claude\temp\f_step44_worker_probe.sh' '/tmp/f_step44_worker_probe.sh'
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'bash /tmp/f_step44_worker_probe.sh'
Remove-Item D:\Claude\temp\f_step44_worker_probe.sh
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'rm -f /tmp/f_step44_worker_probe.sh'
```

# Uploader probe（v0.10 改临时脚本，与 §6.2 同款）—— 见下方 §4.4 Uploader probe 段
# **v0.9 错误纠正**：v0.9 残留 inline `python -c '...'` 多行 + 单引号嵌套，v0.10 改为
# 临时脚本格式（与 §6.2 OSS uploader probe 共用相同脚本，避免重复维护两套）
```

**v0.3 A0a checksum 校验（v0.10 改临时脚本：含 grep 正则单引号嵌套）：**

**`f_step44_a0a_checksum.sh`**：

```bash
#!/bin/bash
# F.2 §4.4 — A0a sample_uploader.py 不含 ResponseContentType= 生效行
# 容器路径 /opt/gateway/（不是 /app/gateway/）
set -euo pipefail

if docker exec aivideotrans-gateway grep -nE 'ResponseContentType\s*=' \
       /opt/gateway/cosyvoice_clone/sample_uploader.py; then
    echo "ERROR: ResponseContentType= 生效行被 grep 命中——A0a fix 丢失，立即 rollback"
    exit 1
fi
echo "OK: no ResponseContentType= active code (only comment markers may exist)"
```

```powershell
& 'D:\daili\scripts\SCP-US-Via-154.cmd' 'D:\Claude\temp\f_step44_a0a_checksum.sh' '/tmp/f_step44_a0a_checksum.sh'
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'bash /tmp/f_step44_a0a_checksum.sh'
Remove-Item D:\Claude\temp\f_step44_a0a_checksum.sh
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'rm -f /tmp/f_step44_a0a_checksum.sh'
```

任一健康检查或 A0a 校验失败 → §9.2 Level 1 rollback。

### 4.5 App rebuild + recreate

**Phase 4.2 改动落在 `src/`（已 bind mount）+ gateway/（已在 §4.4 处理）。**app 容器在 dev mode 下仍是 bind mount，所以**不需要 rebuild image**——`docker compose restart` 即可热加载新 src。

```bash
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'cd /opt/aivideotrans &&  docker compose --env-file /opt/aivideotrans/config/.env restart app'

# 等待 Job API 起来（v0.10 单行 loop）
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'for i in 1 2 3 4 5 6; do curl -s http://localhost:8877/health && break || sleep 5; done'
```

**⚠️ Dev mode 的代价（CLAUDE.md 提到）：** 项目接近完成时必须切回镜像不可变模式。**F 不做这个切换**——切回不可变是独立 task，留 Phase 5 或专项。F.2 保留 bind mount 配置不动。

### 4.6 Next.js rebuild + recreate

**compose 已有 `next` service（`docker-compose.yml:118-134`）**，所以前端走 compose build context、**不**手工铺 standalone：

```bash
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'cd /opt/aivideotrans &&  docker compose --env-file /opt/aivideotrans/config/.env build next &&  docker compose --env-file /opt/aivideotrans/config/.env up -d --no-deps --force-recreate next'

# 等待 next ready（v0.10 单行 loop）
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'for i in 1 2 3 4 5 6; do curl -sI http://localhost:3000/ | head -1 && break || sleep 5; done'
```

**期望：** Caddy 反代下 `https://aitrans.video/workspace/<job_id>` 可访问，CosyVoice 克隆 modal 含 file + segments 两个 radio（E.2 picker 渲染）。

### 4.7 部署间隙节奏

- §4.3 同步：~30 秒
- §4.4 gateway rebuild + healthz：~3-5 分钟（含 image build）
- §4.5 app restart：~15 秒
- §4.6 next rebuild + healthz：~2-3 分钟
- 总窗口：**6-10 分钟**

每步 healthz 不绿不进下一步。

---

## §5 F.3 Admin-only 初始状态验证

### 5.1 admin_settings effective 值校验（v0.5 重写：用 endpoint 看 effective，不读 raw JSON）

**v0.4 错误：** 用 `cat /opt/aivideotrans/config/admin_settings.json | jq` 读 raw JSON。问题：

1. 旧 prod JSON 文件**可能没有**新字段（`cosyvoice_clone_general_availability_enabled` 是 Phase 4.2 加的），jq 会输出 `null`，但 Pydantic 启动时**默认 false**——raw JSON null 不代表 effective null
2. `cosyvoice_clone_worker_enabled` 默认值 `gateway/admin_settings.py:196` 是 `bool = False`，**当前 prod 大概率根本没设过**——raw JSON 可能完全没这个 key
3. Codex P1 #1 指出：admin UI 不暴露 `worker_enabled` toggle，所以 prod 不会自然有这个值

**v0.5 正确做法：** 走 admin endpoint `GET /api/admin/settings`（`gateway/admin_settings.py:342`），看 **effective 值**（Pydantic 默认填充后的值），并明确"若缺/false，先 init"流程。

**Step 1：读 effective 值（v0.7 修正：endpoint 返 `{"settings": {...}}` 包裹层，jq 必须先 `.settings |` 再字段筛选）**

```bash
# 需要 admin session cookie（见 §2.0.0）
# v0.9 改临时脚本：含 jq 单引号 + 双引号嵌套 cookie 读取，inline 太脆
```

**Step 1 临时脚本 `f_step51_admin_settings_check.sh`**：

```bash
#!/bin/bash
# F.3 §5.1 Step 1 — 读 admin_settings effective 值（.settings 子层）
set -euo pipefail

DEPLOY_TS=$(cat /opt/aivideotrans/data/runtime_logs/current_phase42_deploy_ts)
DEPLOY_DIR=/opt/aivideotrans/data/runtime_logs/deploy_${DEPLOY_TS}

curl -s -b "$(cat ${DEPLOY_DIR}/admin_session.txt)" \
     http://localhost:8880/api/admin/settings \
  | jq '.settings | {
      cosyvoice_clone_worker_enabled,
      cosyvoice_clone_user_allowlist,
      cosyvoice_clone_general_availability_enabled
    }'
```

```powershell
& 'D:\daili\scripts\SCP-US-Via-154.cmd' 'D:\Claude\temp\f_step51_admin_settings_check.sh' '/tmp/f_step51_admin_settings_check.sh'
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'bash /tmp/f_step51_admin_settings_check.sh'
Remove-Item D:\Claude\temp\f_step51_admin_settings_check.sh
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'rm -f /tmp/f_step51_admin_settings_check.sh'
```

**出处：** `gateway/admin_settings.py:347` `return {"settings": load_settings().model_dump()}` —— 顶层是 `{"settings": {...}}` 包裹。Codex 六审已 grep 锁。

**期望（F.3 admin-only stage 1 阶段）：**

```json
{
  "cosyvoice_clone_worker_enabled": true,
  "cosyvoice_clone_user_allowlist": [],
  "cosyvoice_clone_general_availability_enabled": false
}
```

**字段语义（v0.5 修正）：**
- `cosyvoice_clone_worker_enabled` —— **总开关**。默认 `False`；F.3 阶段**必须 true** 才能让 admin 用得起来。若发现是 false → 走 Step 2 init
- `cosyvoice_clone_user_allowlist` —— **可以为空数组**。Admin 走 `is_admin` role 分支（`authorization_reason="admin"`），**不需要**写进 allowlist。Allowlist 是给非 admin 灰度白名单用的，F.3 阶段没有
- `cosyvoice_clone_general_availability_enabled` —— F.3 必须 false；缺字段 = effective false 也算通过（不要因为 raw JSON 没有 key 就误判失败）

**Step 2：若 `worker_enabled != true` → init（v0.6 重写：禁止 partial body POST）**

> **⚠️ v0.6 关键修正（Codex 五审 P1）**
>
> v0.5 用 `POST /api/admin/settings -d '{"cosyvoice_clone_worker_enabled": true}'` partial body 是**严重 bug 风险**：
>
> - `admin_settings.py:350-377` 明文标注 endpoint 是 **FULL BODY SEMANTICS**：缺失字段 Pydantic 自动填默认值再 save，**partial body = 静默把所有未传字段重置为默认值**
> - 该 endpoint 还挂 `require_same_origin_state_change` CSRF 守卫，无 `Origin: https://aitrans.video` header 会被 **403 csrf_origin_rejected** 拒收
> - 这两点叠加：v0.5 命令在生产要么直接 403 失败、要么把 admin_settings.json 的其他几十个字段全部重置为默认（含 smart auto-clone / pricing flags 等其他生产配置）
>
> **v0.6 路径**：cosyvoice_clone_worker_enabled 是 hidden ops 开关、admin UI 不暴露，最稳的做法是直接 CLI JSON patch + 重启 gateway，**不**走 API。

**正式步骤（CLI JSON patch，唯一推荐路径；v0.9 改临时脚本格式）：**

**Step 2 临时脚本 `f_step51_worker_enable.sh`**：

```bash
#!/bin/bash
# F.3 §5.1 Step 2 — 备份 admin_settings + jq 设 worker_enabled=true / general_availability=false + restart gateway
set -euo pipefail

DEPLOY_TS=$(cat /opt/aivideotrans/data/runtime_logs/current_phase42_deploy_ts)
DEPLOY_DIR=/opt/aivideotrans/data/runtime_logs/deploy_${DEPLOY_TS}

# 1) 备份当前 admin_settings.json 到部署 log 目录
cp /opt/aivideotrans/config/admin_settings.json \
   ${DEPLOY_DIR}/admin_settings_pre_worker_enabled.json

# 2) jq 写两个字段（v0.5 决策：worker_enabled=true 总开关 + GA 仍 false）
cd /opt/aivideotrans/config
jq '.cosyvoice_clone_worker_enabled = true | .cosyvoice_clone_general_availability_enabled = false' \
   admin_settings.json > admin_settings.json.new
mv admin_settings.json.new admin_settings.json

# 3) restart gateway 让 Pydantic settings 从 JSON 重读
cd /opt/aivideotrans
docker compose --env-file /opt/aivideotrans/config/.env restart gateway

echo "OK: worker_enabled=true persisted + gateway restarted"
```

```powershell
& 'D:\daili\scripts\SCP-US-Via-154.cmd' 'D:\Claude\temp\f_step51_worker_enable.sh' '/tmp/f_step51_worker_enable.sh'
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'bash /tmp/f_step51_worker_enable.sh'
Remove-Item D:\Claude\temp\f_step51_worker_enable.sh
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'rm -f /tmp/f_step51_worker_enable.sh'
# 等 ≥ 5 秒后重跑 Step 1 校验 effective 值
```

**禁止做法（v0.5 写法）：**

```bash
# ❌ 禁止：partial body POST
curl -X POST -d '{"cosyvoice_clone_worker_enabled": true}' /api/admin/settings
# 后果 A：403 csrf_origin_rejected（无 Origin header）
# 后果 B：即便加 Origin pass 了 CSRF，partial body 会让 Pydantic 把所有未传字段
#         重置为默认值——admin_settings.json 里 smart_auto_clone_enabled /
#         smart_reuse_user_voice_enabled / pricing toggle / review_prompts 等
#         全部静默被重置；回归测试 test_post_settings_with_missing_phase3_fields_resets_them_to_defaults
#         明文锁了这条行为。
```

**若坚持走 API 路径（不推荐，仅作备选记录）：**

> **⚠️ v0.9 标注**：以下 (a)/(b)/(c) 三步是**伪代码示例**，**不**提供完整可执行版本——
> Codex 八审 P1-1 要求活命令一律走临时脚本。如果真要走 API 路径，需要按 §2.0.0
> 复杂命令模式拆出 `f_step51_api_full_post.sh` 临时脚本（含 GET + jq.settings 修改 +
> 落到 `${DEPLOY_DIR}/admin_settings_full.json` + diff + POST with `Origin` header）。
> 但**强烈推荐**用上面的 CLI patch 路径而非 API。

伪代码示意（**不**复制执行）：
- (a) GET full → `jq '.settings | .cosyvoice_clone_worker_enabled=true | .cosyvoice_clone_general_availability_enabled=false'` → 落到 `${DEPLOY_DIR}/admin_settings_full.json`
- (b) `diff` 当前 settings vs 修改后 settings — 必须只差两字段
- (c) POST 整 body + `Origin: https://aitrans.video` header + Content-Type: application/json

API 路径的复杂度（GET-modify-diff-POST + Origin header + cookie + 完整 body）远超 CLI jq + restart 的两步——除非未来 admin UI 加了这个 toggle 让它走天然 full-body save，否则 F 阶段一律 CLI。

**Layer 关系（v0.4 §5.1 已有，不变）：**
1. `worker_enabled=false` → 全员不可用（含 admin）
2. `worker_enabled=true` + admin → 可用（`authorization_reason="admin"`）
3. `worker_enabled=true` + 在 allowlist → 可用（`authorization_reason="allowlist"`）
4. `worker_enabled=true` + `general_availability_enabled=true` → 全员可用

**字段含义：**
- `cosyvoice_clone_worker_enabled` —— **总开关**（Layer 2 hard kill 的开关）；关 → 全部 cosyvoice clone 路径不可用
- `cosyvoice_clone_user_allowlist` —— stage 1 灰度的 user_id 字符串数组；admin 默认通过 `is_admin` 走 `authorization_reason="admin"`，allowlist 是给非 admin 灰度白名单准备的
- `cosyvoice_clone_general_availability_enabled` —— GA flip 开关，stage 2 全开

**Layer 关系：**
1. `worker_enabled=false` → 全员不可用（含 admin）
2. `worker_enabled=true` + `general_availability_enabled=false` + user 不在 allowlist 且非 admin → 不可用
3. `worker_enabled=true` + admin → 可用（`authorization_reason="admin"`）
4. `worker_enabled=true` + 在 allowlist → 可用（`authorization_reason="allowlist"`）
5. `worker_enabled=true` + `general_availability_enabled=true` → 全员可用（`authorization_reason="general_availability"`）

**v0.1 错误纠正：** v0.1 写的 `cosyvoice_clone_admin_only` / `cosyvoice_clone_allowlist` 字段**不存在**。Layer 2 hard kill 是 flip `cosyvoice_clone_worker_enabled=false`，不是改不存在的字段。

若 `general_availability_enabled=true` → **立即 flip 回 false**（推荐通过 admin UI，CLI 是 fallback；见 §7.1），重启 gateway，因为部署默认应该是 admin-only stage 1。

### 5.2 两账号黑盒验证

| 账号 | 期望行为 |
|---|---|
| Admin (`js5559sun@proton.me`) | `GET /clone-gate` 返 `can_access_clone=true, runtime_ready=true, can_show_clone_button=true`；UI 看到「克隆音色」按钮亮 |
| 普通测试账号（先建一个） | `GET /clone-gate` 返 `can_access_clone=false, authorization_reason="none"`；UI **看不到**「克隆音色」按钮（D.2 / E.1 wiring 守卫已确保）|

**操作：** 浏览器分别登录，访问任一 succeeded 任务的 voice selection 面板，截图。

### 5.3 通过标准

- admin_settings.json 三个字段对齐 §5.1 期望
- admin 见按钮 + 普通用户不见
- 两账号 clone-gate 返回符合表格

**任一不对 → F 停止**，调查后才能继续 F.4。

---

## §6 F.4 Admin 真烟测（**决定能否 GA**）

### 6.1 clone-gate API（v0.9 改临时脚本：读 cookie 文件 + 单引号嵌套避免）

**`f_step61_clone_gate.sh`**：

```bash
#!/bin/bash
# F.4 §6.1 — admin clone-gate API smoke
set -euo pipefail

DEPLOY_TS=$(cat /opt/aivideotrans/data/runtime_logs/current_phase42_deploy_ts)
DEPLOY_DIR=/opt/aivideotrans/data/runtime_logs/deploy_${DEPLOY_TS}

curl -s -b "$(cat ${DEPLOY_DIR}/admin_session.txt)" \
     https://aitrans.video/api/voice/cosyvoice/clone-gate \
  | jq .
```

```powershell
& 'D:\daili\scripts\SCP-US-Via-154.cmd' 'D:\Claude\temp\f_step61_clone_gate.sh' '/tmp/f_step61_clone_gate.sh'
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'bash /tmp/f_step61_clone_gate.sh'
Remove-Item D:\Claude\temp\f_step61_clone_gate.sh
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'rm -f /tmp/f_step61_clone_gate.sh'
```

**期望：**
- `can_access_clone: true`
- `runtime_ready: true`
- `runtime_unavailable_code: null`
- `can_show_clone_button: true`
- `authorization_reason: "admin"`

### 6.2 OSS uploader probe（v0.9 改临时脚本：python -c 多行 + 单引号嵌套避免）

**`f_step62_oss_uploader_probe.sh`**（v0.4 修正：gateway 容器 import 不带 `gateway.` 前缀）：

```bash
#!/bin/bash
# F.4 §6.2 — OSS uploader factory probe
# gateway container WORKDIR=/opt/gateway → 包路径无 `gateway.` 前缀
set -euo pipefail

docker exec -i aivideotrans-gateway python3 <<'PYEOF'
import hashlib
import io
import urllib.error
import urllib.request
import wave

from config import settings
from cosyvoice_clone.sample_uploader import (
    build_sample_uploader_from_settings,
    missing_aliyun_oss_settings,
)
missing = missing_aliyun_oss_settings(settings)
print({"missing_settings": missing})
u = build_sample_uploader_from_settings(settings)
print({
    "impl": type(u).__name__,
    "bucket": settings.cosyvoice_oss_bucket,
    "endpoint": settings.cosyvoice_oss_endpoint,
})

buf = io.BytesIO()
with wave.open(buf, "wb") as wav:
    wav.setnchannels(1)
    wav.setsampwidth(2)
    wav.setframerate(16000)
    wav.writeframes(b"\x00\x00" * 16000 * 5)
data = buf.getvalue()
expected_sha256 = hashlib.sha256(data).hexdigest()

url = u.upload_and_sign(data, filename_hint="phase42_oss_probe.wav", ttl_seconds=600)
print({"signed_url_prefix": url[:80], "uploaded_bytes": len(data)})

downloaded = urllib.request.urlopen(url, timeout=20).read()
actual_sha256 = hashlib.sha256(downloaded).hexdigest()
if actual_sha256 != expected_sha256:
    raise SystemExit(f"OSS round-trip sha mismatch: {actual_sha256} != {expected_sha256}")

u.delete_uploaded_url(url)
try:
    urllib.request.urlopen(url, timeout=20).read()
    raise SystemExit("OSS delete check failed: signed URL still returned 200 after delete")
except urllib.error.HTTPError as exc:
    if exc.code not in (403, 404):
        raise

print({"oss_round_trip": "PASS", "sha256": expected_sha256})
PYEOF
```

```powershell
& 'D:\daili\scripts\SCP-US-Via-154.cmd' 'D:\Claude\temp\f_step62_oss_uploader_probe.sh' '/tmp/f_step62_oss_uploader_probe.sh'
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'bash /tmp/f_step62_oss_uploader_probe.sh'
Remove-Item D:\Claude\temp\f_step62_oss_uploader_probe.sh
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'rm -f /tmp/f_step62_oss_uploader_probe.sh'
```

**期望：** `missing_settings: []` + `impl: AliyunOssUploader` + `oss_round_trip: PASS`。这一步必须真实 PUT → signed GET → sha256 校验 → DELETE → 删除后 403/404。

v0.15 修正：`get_uploader().healthz()` 是虚构 API；真实工厂是 `build_sample_uploader_from_settings(settings)`，且没有 `healthz()` 方法。本步骤不再只做 factory/config probe，而是强制做真实 OSS PUT/GET/DELETE round-trip，避免到真克隆时才发现 signed URL 或删除路径不可用。

### 6.3 武汉 worker 探活（v0.3 修正：用 admin healthz endpoint，不引虚构脚本）

仓库**没有** `scripts/worker_smoke.py`（v0.1/v0.2 引用的是虚构脚本）。F.4 worker probe 改用既有 admin endpoint：

**`f_step63_worker_healthz.sh`**（v0.9 改临时脚本）：

```bash
#!/bin/bash
# F.4 §6.3 — 武汉 worker /healthz 探活
set -euo pipefail

DEPLOY_TS=$(cat /opt/aivideotrans/data/runtime_logs/current_phase42_deploy_ts)
DEPLOY_DIR=/opt/aivideotrans/data/runtime_logs/deploy_${DEPLOY_TS}

curl -s -b "$(cat ${DEPLOY_DIR}/admin_session.txt)" \
     http://localhost:8880/api/admin/mainland-voice-worker/healthz \
  | jq .
```

```powershell
& 'D:\daili\scripts\SCP-US-Via-154.cmd' 'D:\Claude\temp\f_step63_worker_healthz.sh' '/tmp/f_step63_worker_healthz.sh'
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'bash /tmp/f_step63_worker_healthz.sh'
Remove-Item D:\Claude\temp\f_step63_worker_healthz.sh
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'rm -f /tmp/f_step63_worker_healthz.sh'
```

**期望（成功路径）：** 200 + 含 `ok / worker / region / providers` 字段（来自 worker 自身 `/healthz`，gateway 转发不附 secret）。

**失败路径与排障：**
- `worker_disabled` 503 → 检查 `AVT_MAINLAND_VOICE_WORKER_ENABLED=true`
- 网络不通 → 检查 WG handshake `wg show`，参考 task #94 systemd unit 状态
- HMAC 错 → 比对 US gateway `AVT_MAINLAND_VOICE_WORKER_HMAC_KEY_ID/SECRET` 与武汉 worker 端 keyring

**未来补强（不在 F 范围）：** 真正的 worker 烟测脚本（dispatch + silent WAV 校验 + 端到端 HMAC 验证）作为独立工具，独立专项做。

### 6.4 真克隆一次（**唯一允许的付费 API 调用**）

**Setup：**
- Admin 账号
- 一段 5 秒 mp3 / wav 样本（自录，本人朗读，安静环境）
- 任选一个 admin 自己的 succeeded 任务（voice 选择面板）

**操作：**
1. 点「克隆音色」按钮 → CosyVoice modal 打开
2. 输入音色名 `Phase42 烟测 Admin <yyyy-mm-dd>`
3. 选 target model `cosyvoice-v3.5-flash`
4. sample mode = file（这次先测 file 路径；source_segments 在 §6.5 补）
5. 选音频文件 → 文件 5 维校验通过
6. 点「提交克隆」→ Consent modal 全 3 勾选 → 「开始克隆」
7. 等待 ≤ 60 秒，期望 toast「克隆成功，已加入个人音色库」

**校验产物（v0.5 修正：精确断言，与 `gateway/cosyvoice_clone/api.py:970-998` 真实 INSERT 完全对齐）：**

```sql
-- a. user_voices row 落地（file 路径）
SELECT id, voice_id, provider, tts_provider, platform,
       target_model, clone_api_model,
       is_temporary, temporary_expires_at,
       clone_sample_seconds, clone_sample_segment_ids,
       requires_worker, worker_provider, worker_region, region_constraint,
       billing_sku,
       clone_provider_request_id, clone_worker_request_id,
       source_speaker_id, source_job_id, created_from
FROM user_voices
WHERE user_id = '<admin uuid>'   -- TEMPLATE ONLY - DO NOT RUN: 替换为 §6.7 cleanup_inputs.json.admin_user_id
ORDER BY created_at DESC LIMIT 1;
```

**v0.11 修正（Codex 十审 P2）：** 上面 SQL 是**查阅模板**，不是 runbook 直接执行的命令。`<admin uuid>` 标注为 `TEMPLATE ONLY - DO NOT RUN`；真实执行用 §6.7 `cleanup_inputs.json` 的 `admin_user_id` 字段，或直接用 admin 后台 UI 查 user_voices 列表（admin 后台 → 用户管理）。

**期望（file 路径，每个字段都来自 grep 出处）：**

| 字段 | 期望值 | 出处 |
|---|---|---|
| `provider` | `'cosyvoice_voice_clone'` | `api.py:975` `PROVIDER_COSYVOICE_VOICE_CLONE` |
| `tts_provider` | `'cosyvoice'` | `api.py:976` `TTS_PROVIDER_COSYVOICE` |
| `platform` | `'dashscope_mainland'` 或同名常量 | `api.py:977` `PLATFORM_DASHSCOPE_MAINLAND` |
| `target_model` | `'cosyvoice-v3.5-flash'` | `api.py:990`（用户提交值）|
| `clone_api_model` | 非空字符串 | `api.py:993` `CLONE_API_MODEL` |
| `is_temporary` | `TRUE` | 默认（用户没勾"保存"）|
| `temporary_expires_at` | now() + ~7 天 | A.1 默认 |
| `clone_sample_seconds` | ≈ 5.0 | `api.py:981-984`，duration_ms/1000 |
| `clone_sample_segment_ids` | **IS NULL** | `api.py:985`，file 路径 `parsed_segments` 为空 |
| `requires_worker` | `TRUE` | `api.py:989` 写死 |
| `worker_provider` | **`'cosyvoice'`**（**不是** `'cosyvoice_voice_clone'`，v0.4 写错）| `api.py:991` `WORKER_PROVIDER_COSYVOICE` |
| `worker_region` | `'cn_wuhan'` 或同名常量 | `api.py:992` `WORKER_REGION_CN_WUHAN` |
| `region_constraint` | `'mainland_only'` 或同名常量 | `api.py:988` `REGION_CONSTRAINT_MAINLAND_ONLY` |
| `billing_sku` | **IS NULL** | `api.py:994-995` 注释："等首次实账单回填"——首烟测时**不会**有值 |
| `clone_provider_request_id` | 非空 UUID（DashScope）| `api.py:996` |
| `clone_worker_request_id` | 非空 UUID（武汉 worker）| `api.py:997` |
| `created_from` | `'cosyvoice_clone_endpoint'` | `api.py:986` |

**v0.4 错误纠正（Codex 四审 P1 #2）：**
- `worker_provider` 写 `'cosyvoice_voice_clone' 或 'cosyvoice'` 不对——grep `api.py:991` 是 `WORKER_PROVIDER_COSYVOICE` 字面 `'cosyvoice'`，**没有** `'cosyvoice_voice_clone'` 这个值在 worker_provider 上
- `billing_sku` 写"含 `cosyvoice-v3.5-flash`"完全错——`api.py:994-995` 注释明确 "Codex 2026-05-25 三轮决策：等首次实账单回填"，**首次烟测必然 NULL**
- 这两个字段写错会让 F.4 烟测 SQL 直接断言失败，且看起来像"代码 bug"——但实际是 spec 错

```bash
# b. DashScope 出账
# 手动登录 DashScope console 查 admin 账号当日 cosyvoice clone 调用次数 + 费用
# 期望：+1 调用，费用约 ¥0.01

# c. 武汉 worker 日志 / audit JSONL（v0.10 改临时脚本 `f_step64_clone_logs.sh`）
```

**`f_step64_clone_logs.sh`**：

```bash
#!/bin/bash
# F.4 §6.4 c — 真 clone 后 gateway 日志 + audit JSONL 含 worker_request_id
set -euo pipefail

echo "=== gateway docker logs（含 worker_request_id / cosyvoice_clone / target_model）==="
docker logs aivideotrans-gateway --tail 100 | grep -E '(worker_request_id|cosyvoice_clone|target_model)' || echo "(no matches)"

echo
echo "=== audit JSONL（clone event）==="
tail -100 /opt/aivideotrans/data/runtime_logs/audit_*.jsonl 2>/dev/null \
  | jq 'select(.event_type | contains("clone"))' || echo "(no matches)"
```

```powershell
& 'D:\daili\scripts\SCP-US-Via-154.cmd' 'D:\Claude\temp\f_step64_clone_logs.sh' '/tmp/f_step64_clone_logs.sh'
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'bash /tmp/f_step64_clone_logs.sh'
Remove-Item D:\Claude\temp\f_step64_clone_logs.sh
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'rm -f /tmp/f_step64_clone_logs.sh'
```

**期望：**
- gateway 日志含 `worker_request_id: <uuid>` + `target_model: cosyvoice-v3.5-flash`
- runtime_logs audit JSONL 含一行 clone audit（含 user_id / job_id / sample_seconds / billed_chars=0 等）

### 6.5 真 TTS 路由烟测

在同一任务下：
1. Voice selection 把 admin 刚克隆的 voice 选给某个 speaker
2. 提交 voice selection → 进入 pipeline TTS 阶段
3. 等 TTS 一段完成

**校验：**

**`f_step65_tts_routing_logs.sh`**（v0.10 改临时脚本）：

```bash
#!/bin/bash
# F.4 §6.5 a — TTS pipeline 日志含 routing fields
set -euo pipefail

docker logs aivideotrans-app --tail 200 \
  | grep -E '(requires_worker|worker_target_model|_generate_one_cosyvoice_via_worker)' \
  || echo "(no matches —— 检查 pipeline 是否真跑到 TTS 阶段)"
```

```powershell
& 'D:\daili\scripts\SCP-US-Via-154.cmd' 'D:\Claude\temp\f_step65_tts_routing_logs.sh' '/tmp/f_step65_tts_routing_logs.sh'
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'bash /tmp/f_step65_tts_routing_logs.sh'
Remove-Item D:\Claude\temp\f_step65_tts_routing_logs.sh
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'rm -f /tmp/f_step65_tts_routing_logs.sh'
```

**期望：**
- 日志含 `requires_worker=True`
- 日志含 `worker_target_model=cosyvoice-v3.5-flash`
- 日志含 `_generate_one_cosyvoice_via_worker` 路径调用
- **不**出现 `MiniMax` / `VolcEngine` 路径（要确保 cosyvoice clone voice 没串到 MiniMax 路径）

**v0.11 修正（Codex 十审 P1-2）：** v0.10 写 `<job_id>` 占位活命令易错。v0.11 改为 admin 烟测开始时把 job_id 落到 `${DEPLOY_DIR}/smoke_job_id.txt`，命令读取文件：

**`f_step65_smoke_job_id_save.sh`**（在 §6.5 真 TTS 测试开始前跑一次）：

```bash
#!/bin/bash
# F.4 §6.5 — 保存 smoke job_id 到部署 log 目录，供后续 §6.5/§6.8 cleanup 读取
set -euo pipefail

DEPLOY_TS=$(cat /opt/aivideotrans/data/runtime_logs/current_phase42_deploy_ts)
DEPLOY_DIR=/opt/aivideotrans/data/runtime_logs/deploy_${DEPLOY_TS}

# 执行前替换 <smoke_job_id> 为 §6.4 / §6.5 admin 实际操作的 job id
SMOKE_JOB_ID="<smoke_job_id>"
echo "${SMOKE_JOB_ID}" > ${DEPLOY_DIR}/smoke_job_id.txt
echo "OK: smoke job id saved to ${DEPLOY_DIR}/smoke_job_id.txt = ${SMOKE_JOB_ID}"
```

```powershell
# 1) 本地替换 SMOKE_JOB_ID 占位为 §6.4 admin 操作的 job id
# 2) 推 + 跑
& 'D:\daili\scripts\SCP-US-Via-154.cmd' 'D:\Claude\temp\f_step65_smoke_job_id_save.sh' '/tmp/f_step65_smoke_job_id_save.sh'
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'bash /tmp/f_step65_smoke_job_id_save.sh'
Remove-Item D:\Claude\temp\f_step65_smoke_job_id_save.sh
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'rm -f /tmp/f_step65_smoke_job_id_save.sh'
```

**`f_step65_tts_segments_ls.sh`**（替代 v0.10 的 `<job_id>` 活命令）：

```bash
#!/bin/bash
# F.4 §6.5 b — 列 TTS 产物（job_id 从 smoke_job_id.txt 读，不再占位）
set -euo pipefail

DEPLOY_TS=$(cat /opt/aivideotrans/data/runtime_logs/current_phase42_deploy_ts)
DEPLOY_DIR=/opt/aivideotrans/data/runtime_logs/deploy_${DEPLOY_TS}

JOB_ID=$(cat ${DEPLOY_DIR}/smoke_job_id.txt)
ls -la /opt/aivideotrans/app/jobs/${JOB_ID}/tts_segments_aligned/
```

```powershell
& 'D:\daili\scripts\SCP-US-Via-154.cmd' 'D:\Claude\temp\f_step65_tts_segments_ls.sh' '/tmp/f_step65_tts_segments_ls.sh'
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'bash /tmp/f_step65_tts_segments_ls.sh'
Remove-Item D:\Claude\temp\f_step65_tts_segments_ls.sh
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'rm -f /tmp/f_step65_tts_segments_ls.sh'
```

**期望：** wav 文件大小 > 1 KB，duration ≈ 段长。

### 6.6 source_segments 路径补烟测

E.2 新增的第二输入路径也要走通：

1. 同一任务，打开克隆 modal
2. 切到 segments 模式 → picker 出现
3. 勾选 2 段加起来 ≥ 3 秒 + ≤ 60 秒
4. 提交 → consent → 提交

**校验（v0.3 修正：用 `clone_sample_segment_ids` 区分）：**

```sql
-- TEMPLATE ONLY - DO NOT RUN: 替换 <admin uuid> 为 §6.7 cleanup_inputs.json.admin_user_id
SELECT clone_sample_segment_ids, clone_sample_seconds, target_model, is_temporary
FROM user_voices
WHERE user_id = '<admin uuid>'
ORDER BY created_at DESC LIMIT 1;
```

**期望（segments 路径）：**
- **`clone_sample_segment_ids` 非空**（JSONB 数组，含真实段号，例如 `[3, 7, 11]`）——这是 segments 路径的 distinguishing marker
- `clone_sample_seconds` ≈ 选段总时长（拼接后实际秒数）
- `target_model = 'cosyvoice-v3.5-flash'`
- `is_temporary = TRUE`

### 6.7 F.4 通过标准（全部必须绿）

- [ ] 6.1 clone-gate 返回符合期望
- [ ] 6.2 OSS uploader factory/config + 真实 PUT/GET/DELETE round-trip ok（v0.15 收口：不再把 OSS 端到端验证留到 F.6 之后）
- [ ] 6.3 worker smoke ok
- [ ] 6.4 真 clone（file）成功 + user_voices row + DashScope 出账 + worker_request_id 日志
- [ ] 6.5 真 TTS 走 worker 路径
- [ ] 6.6 真 clone（segments）成功 + clone_sample_segment_ids 落地
- [ ] runtime_logs/ 有可审计的 audit JSONL

**任一项红 → F 停止 GA flip**，整改后重测全套 6.1-6.6。

### 6.8 Admin 烟测产物手动 cleanup SOP（**v0.2 新增**，Codex P1 #4）

**背景：** 全仓 grep 确认 `temporary_expires_at` 目前**只有** schema/ORM/test/spec，**没有** runtime sweeper（cron / admin trigger / background task 都没实现）。所以 §6.4 / §6.6 烟测产生的 admin 克隆 voice **必须手动清理**——sweeper 启动是独立专项，不在 F 范围。

**为什么必须清理：**
- 占 DashScope 平台 voice 数量（每个账号有上限）
- 占 user_voices 表 row（admin 收 voice library 列表干扰）
- OSS 上有样本文件（保留 sample 与最小化数据原则不符）

**Cleanup 流程（v0.11 重写：先收集 inputs.json，cleanup 脚本读 JSON，无人工占位编辑）：**

### Step 1 / 收集 cleanup inputs（部署完后立即跑，v0.11 新增）

**`f_step67_collect_cleanup_inputs.sh`** — 从 user_voices + audit JSONL 自动导出 cleanup_inputs.json：

```bash
#!/bin/bash
# F.6 §6.8 Step 1 — 收集 admin 烟测产物用于 cleanup 的输入数据
# 输出：${DEPLOY_DIR}/cleanup_inputs.json
# 字段：
#   admin_user_id (string)
#   voices: [
#     {user_voice_id, voice_id, target_model, clone_sample_segment_ids, created_at, oss_url?},
#     ...
#   ]
set -euo pipefail

DEPLOY_TS=$(cat /opt/aivideotrans/data/runtime_logs/current_phase42_deploy_ts)
DEPLOY_DIR=/opt/aivideotrans/data/runtime_logs/deploy_${DEPLOY_TS}

# 1) admin user_id —— 从 sessions 表查（admin 邮箱 js5559sun@proton.me）
ADMIN_USER_ID=$(docker exec aivideotrans-postgres psql -U avt -d aivideotrans -t -A -c \
    "SELECT id::text FROM users WHERE email = 'js5559sun@proton.me' LIMIT 1;")
if [[ -z "${ADMIN_USER_ID}" ]]; then
    echo "ERROR: 未找到 admin user (email=js5559sun@proton.me)；inputs 收集失败"
    exit 1
fi
echo "ADMIN_USER_ID=${ADMIN_USER_ID}"
export ADMIN_USER_ID

# 2) 烟测时间窗内的 admin cosyvoice clone voices
#    时间窗：deploy_${DEPLOY_TS} 起 4 小时内（包含 §6.4 file path + §6.6 segments path）
DEPLOY_HUMAN=$(echo "${DEPLOY_TS}" | sed 's/_/ /; s/\(.\{4\}\)\(.\{2\}\)\(.\{2\}\) /\1-\2-\3 /; s/\(.\{2\}\)\(.\{2\}\)\(.\{2\}\)$/\1:\2:\3/')

# 用 jq 直接构造 JSON
docker exec aivideotrans-postgres psql -U avt -d aivideotrans -t -A -F'|' -c "
SELECT
    id::text,
    voice_id,
    target_model,
    COALESCE(clone_sample_segment_ids::text, 'null'),
    created_at::text
FROM user_voices
WHERE user_id = '${ADMIN_USER_ID}'
  AND provider = 'cosyvoice_voice_clone'
  AND tts_provider = 'cosyvoice'
  AND platform = 'dashscope_mainland'
  AND created_from = 'cosyvoice_clone_endpoint'
  AND requires_worker IS TRUE
  AND worker_provider = 'cosyvoice'
  AND target_model = 'cosyvoice-v3.5-flash'
  AND clone_sample_seconds IS NOT NULL
  AND created_at >= '${DEPLOY_HUMAN}'::timestamptz
  AND created_at <= ('${DEPLOY_HUMAN}'::timestamptz + interval '4 hours')
  AND expired_at IS NULL
ORDER BY created_at DESC;
" > ${DEPLOY_DIR}/_voices_raw.tsv

# 3) OSS URL 可选——从 audit JSONL 抽 oss_url
#    Phase 4.2 audit 事件 type 含 "cosyvoice_clone"；oss_url 字段在事件 payload 里
OSS_URLS_JSON="[]"
if compgen -G "/opt/aivideotrans/data/runtime_logs/audit_*.jsonl" > /dev/null; then
    OSS_URLS_JSON=$(tail -200 /opt/aivideotrans/data/runtime_logs/audit_*.jsonl 2>/dev/null \
        | jq -c -s '[.[] | select(.event_type? // "" | contains("clone")) | .oss_url // empty]' \
        || echo "[]")
fi

# 4) 用 python 拼最终 JSON（jq 难处理 TSV）
python3 - <<PYEOF
import json, os

admin_user_id = os.environ.get("ADMIN_USER_ID")
if not admin_user_id:
    raise SystemExit("ADMIN_USER_ID missing; cleanup_inputs.json would be unsafe")

voices = []
with open("${DEPLOY_DIR}/_voices_raw.tsv", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) < 5:
            continue
        uvid, vid, tm, segids, created = parts[0], parts[1], parts[2], parts[3], parts[4]
        voices.append({
            "user_voice_id": uvid,
            "voice_id": vid,
            "target_model": tm,
            "clone_sample_segment_ids": None if segids == "null" else segids,
            "created_at": created,
        })

if len(voices) != 2:
    raise SystemExit(f"expected exactly 2 smoke voices (file + segments), got {len(voices)}")
if not any(v["clone_sample_segment_ids"] is None for v in voices):
    raise SystemExit("missing file-upload smoke voice (clone_sample_segment_ids IS NULL)")
if not any(v["clone_sample_segment_ids"] is not None for v in voices):
    raise SystemExit("missing source-segments smoke voice (clone_sample_segment_ids IS NOT NULL)")

oss_urls = json.loads('${OSS_URLS_JSON}')

output = {
    "admin_user_id": admin_user_id,
    "voices": voices,
    "oss_urls": oss_urls,
    "deploy_ts": "${DEPLOY_TS}",
}
with open("${DEPLOY_DIR}/cleanup_inputs.json", "w", encoding="utf-8") as f:
    json.dump(output, f, indent=2, ensure_ascii=False)

print(f"OK: wrote ${DEPLOY_DIR}/cleanup_inputs.json")
print(f"  admin_user_id: {admin_user_id}")
print(f"  voices: {len(voices)} rows (validated: exactly 1 file + 1 segments path)")
print(f"  oss_urls: {len(oss_urls)} (optional, may be 0)")
PYEOF

rm -f ${DEPLOY_DIR}/_voices_raw.tsv
```

```powershell
& 'D:\daili\scripts\SCP-US-Via-154.cmd' 'D:\Claude\temp\f_step67_collect_cleanup_inputs.sh' '/tmp/f_step67_collect_cleanup_inputs.sh'
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'bash /tmp/f_step67_collect_cleanup_inputs.sh'
Remove-Item D:\Claude\temp\f_step67_collect_cleanup_inputs.sh
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'rm -f /tmp/f_step67_collect_cleanup_inputs.sh'
```

**期望：** `${DEPLOY_DIR}/cleanup_inputs.json` 落地，`admin_user_id` 非空，且脚本已强制校验 `voices` 数组**恰好 2 行**：1 行 `clone_sample_segment_ids IS NULL`（file path）+ 1 行 `clone_sample_segment_ids IS NOT NULL`（segments path）。任何 0 行 / 1 行 / 多行 / 缺任一路径都必须 fail-fast，不允许进入 delete/soft-delete 步骤。

### Step 2 / Worker delete voices（v0.11 改读 cleanup_inputs.json）

**v0.3 修正背景：** worker DELETE 真实 API 是 `DELETE /cosyvoice/voices/{voice_id}` HMAC（不是虚构的 `/admin/delete_voice` + `X-Worker-Auth`）。Client wrapper：`MainlandWorkerClient.delete_voice(voice_id, WorkerDeleteVoiceRequest(...))`。

**`f_step68_worker_delete_voice.py`**（v0.11 改读 JSON，无人工占位）：

```python
# f_step68_worker_delete_voice.py
# F.6 §6.8 Step 2 — 通过 MainlandWorkerClient 删 admin 烟测产生的所有 cosyvoice voice
# 在 aivideotrans-app 容器内跑（WORKDIR=/opt/aivideotrans/app，PYTHONPATH 含 src/）
# v0.11 修正：从 /opt/.../runtime_logs/deploy_${DEPLOY_TS}/cleanup_inputs.json 读输入
import json
import os
import sys

from src.services.mainland_worker.client_factory import build_client_from_env
from src.services.mainland_worker.types import WorkerDeleteVoiceRequest

# DEPLOY_TS 由外层 bash 命令导入到环境变量
DEPLOY_TS = os.environ.get("DEPLOY_TS") or sys.exit("DEPLOY_TS env not set")
INPUTS = f"/opt/aivideotrans/data/runtime_logs/deploy_{DEPLOY_TS}/cleanup_inputs.json"

with open(INPUTS, encoding="utf-8") as f:
    inputs = json.load(f)

client = build_client_from_env()
if client is None:
    print({"error": "client_factory returned None — worker env incomplete; rerun §2.4 audit"})
    sys.exit(1)

results = []
for v in inputs["voices"]:
    try:
        outcome = client.delete_voice(
            v["voice_id"],
            WorkerDeleteVoiceRequest(
                job_id="f-smoke-cleanup",
                user_id=inputs["admin_user_id"],
                reason="f.4_smoke_test_cleanup",
            ),
        )
        results.append({"voice_id": v["voice_id"], "ok": True, "outcome": str(outcome)})
    except Exception as exc:
        # 失败不阻断后续 voice 删除 —— Codex 八审决议：admin 烟测 voice 留 DashScope 不影响生产
        results.append({"voice_id": v["voice_id"], "ok": False, "error": str(exc)})

print(json.dumps({"results": results}, indent=2, ensure_ascii=False))
```

```powershell
# 推到 app 容器（PYTHONPATH 含 src/）；DEPLOY_TS 由 SSH 命令带进容器 env
& 'D:\daili\scripts\SCP-US-Via-154.cmd' 'D:\Claude\temp\f_step68_worker_delete_voice.py' '/tmp/f_step68_worker_delete_voice.py'
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'DEPLOY_TS=$(cat /opt/aivideotrans/data/runtime_logs/current_phase42_deploy_ts) && docker cp /tmp/f_step68_worker_delete_voice.py aivideotrans-app:/tmp/f_step68_worker_delete_voice.py && docker exec -e DEPLOY_TS=${DEPLOY_TS} aivideotrans-app python3 /tmp/f_step68_worker_delete_voice.py && docker exec aivideotrans-app rm -f /tmp/f_step68_worker_delete_voice.py'
Remove-Item D:\Claude\temp\f_step68_worker_delete_voice.py
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'rm -f /tmp/f_step68_worker_delete_voice.py'
```

**期望：** `results` 数组每项 `ok=true`；DashScope console 显示对应 voice 已删。

**失败处理：** 如某 voice `delete_voice` 抛异常 → 不阻断其他 voice 删除；记录 voice_id，留运维通过 DashScope console 手动 reconcile。

### Step 3 / user_voices soft-delete（v0.11 改读 JSON，无人工占位）

**`f_step68_soft_delete.sh`**：

```bash
#!/bin/bash
# F.6 §6.8 Step 3 — 把 admin 烟测 voice 在 user_voices 表里软删（mark expired，保留审计）
set -euo pipefail

DEPLOY_TS=$(cat /opt/aivideotrans/data/runtime_logs/current_phase42_deploy_ts)
INPUTS=/opt/aivideotrans/data/runtime_logs/deploy_${DEPLOY_TS}/cleanup_inputs.json

# 抽出 user_voice_id 数组
IDS=$(jq -r '.voices[].user_voice_id' ${INPUTS} | paste -sd ',' -)
if [[ -z "${IDS}" ]]; then
    echo "OK: no voices to soft-delete (cleanup_inputs.json empty)"
    exit 0
fi

# 把 'a,b,c' 转成 SQL 'a','b','c'
SQL_IN=$(echo "${IDS}" | sed "s/,/','/g")
SQL_IN="'${SQL_IN}'"

docker exec aivideotrans-postgres psql -U avt -d aivideotrans -c "
UPDATE user_voices
SET expired_at = now(),
    notes = COALESCE(notes, '') || ' [F.4 smoke-test cleanup ${DEPLOY_TS}]'
WHERE id::text IN (${SQL_IN});
"
echo "OK: soft-deleted user_voices rows: ${IDS}"
```

```powershell
& 'D:\daili\scripts\SCP-US-Via-154.cmd' 'D:\Claude\temp\f_step68_soft_delete.sh' '/tmp/f_step68_soft_delete.sh'
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'bash /tmp/f_step68_soft_delete.sh'
Remove-Item D:\Claude\temp\f_step68_soft_delete.sh
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'rm -f /tmp/f_step68_soft_delete.sh'
```

### Step 4 / OSS 样本删除（可选，v0.11 改读 JSON）

OSS 有 lifecycle policy 自动过期；F 阶段不强制。若需要立即删——

**`f_step68_oss_delete.py`**（v0.11 改：循环读 `cleanup_inputs.json` 里的 `oss_urls` 数组）：

```python
# f_step68_oss_delete.py
# F.6 §6.8 Step 4 (optional) — 删除 admin 烟测产生的所有 OSS 样本文件
# 在 aivideotrans-gateway 容器内跑（WORKDIR=/opt/gateway，无 `gateway.` 前缀）
import json
import os

from config import settings
from cosyvoice_clone.sample_uploader import build_sample_uploader_from_settings

DEPLOY_TS = os.environ.get("DEPLOY_TS") or exit("DEPLOY_TS env not set")
INPUTS = f"/opt/aivideotrans/data/runtime_logs/deploy_{DEPLOY_TS}/cleanup_inputs.json"

with open(INPUTS, encoding="utf-8") as f:
    inputs = json.load(f)

oss_urls = inputs.get("oss_urls") or []
if not oss_urls:
    print({"status": "noop", "reason": "no oss_urls in cleanup_inputs.json (OK if audit JSONL did not record oss_url)"})
    exit(0)

u = build_sample_uploader_from_settings(settings)
results = []
for url in oss_urls:
    try:
        u.delete_uploaded_url(url)
        results.append({"oss_url": url, "ok": True})
    except Exception as exc:
        results.append({"oss_url": url, "ok": False, "error": str(exc)})

print(json.dumps({"results": results}, indent=2, ensure_ascii=False))
```

```powershell
& 'D:\daili\scripts\SCP-US-Via-154.cmd' 'D:\Claude\temp\f_step68_oss_delete.py' '/tmp/f_step68_oss_delete.py'
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'DEPLOY_TS=$(cat /opt/aivideotrans/data/runtime_logs/current_phase42_deploy_ts) && docker cp /tmp/f_step68_oss_delete.py aivideotrans-gateway:/tmp/f_step68_oss_delete.py && docker exec -e DEPLOY_TS=${DEPLOY_TS} aivideotrans-gateway python3 /tmp/f_step68_oss_delete.py && docker exec aivideotrans-gateway rm -f /tmp/f_step68_oss_delete.py'
Remove-Item D:\Claude\temp\f_step68_oss_delete.py
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'rm -f /tmp/f_step68_oss_delete.py'
```

**v0.1/v0.2 错误纠正：** 写的 `u.delete(key)` 是虚构 API；实际接口是 `delete_uploaded_url(url)`（`sample_uploader.py:277`）。
**v0.10 → v0.11：** OSS_URL 占位符改为从 cleanup_inputs.json 循环读取，无人工编辑。

**Cleanup 完成确认（`f_step68_verify.sh`，v0.11 改读 cleanup_inputs.json）：**

```bash
#!/bin/bash
# F.6 §6.8 — 验证 admin user 当前 active cosyvoice clone voice 数 = 0
set -euo pipefail

DEPLOY_TS=$(cat /opt/aivideotrans/data/runtime_logs/current_phase42_deploy_ts)
INPUTS=/opt/aivideotrans/data/runtime_logs/deploy_${DEPLOY_TS}/cleanup_inputs.json
ADMIN_USER_ID=$(jq -r '.admin_user_id' ${INPUTS})

COUNT=$(docker exec aivideotrans-postgres psql -U avt -d aivideotrans -t -A -c "
SELECT count(*) FROM user_voices
WHERE user_id = '${ADMIN_USER_ID}'
  AND provider = 'cosyvoice_voice_clone'
  AND expired_at IS NULL;
")
echo "admin active cosyvoice clone voice count: ${COUNT}"
if [[ "${COUNT}" != "0" ]]; then
    echo "ERROR: 期望 0，实际 ${COUNT}——cleanup 未完成或漏 row"
    exit 1
fi
echo "OK: cleanup verified (0 active rows)"
```

```powershell
& 'D:\daili\scripts\SCP-US-Via-154.cmd' 'D:\Claude\temp\f_step68_verify.sh' '/tmp/f_step68_verify.sh'
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'bash /tmp/f_step68_verify.sh'
Remove-Item D:\Claude\temp\f_step68_verify.sh
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'rm -f /tmp/f_step68_verify.sh'
```

**为什么不交给 sweeper：**
- sweeper 还没实现，没有谁会扫这些 row
- `temporary_expires_at` 默认 7 天后；即便 sweeper 实现了，烟测 voice 也要等 7 天才被扫到——同期 admin voice library 一直有干扰
- 手动 cleanup 24h 内做完，干扰窗口短

**记录到 incident-ready 文件：** 全部清理过程截图 + SQL 输出落 `runtime_logs/deploy_<ts>/admin_smoke_cleanup.md`。

---

## §7 F.5 GA flip + Rollback 开关

### 7.1 GA flip 操作（**v0.2 修正：admin UI 优先**，Codex P2）

**触发条件：** F.4 全部 6 项绿，间隔观察 ≥ 30 分钟无新报错。

**主路径：admin UI 翻 flag**（这条路径经 admin auth + StrictBool + full-body save 测试覆盖；纯 jq 改 JSON 可能绕过 in-memory reload）：

1. Admin 登录 → 后台 → CosyVoice clone 管理面板
2. 翻 `general_availability_enabled` 开关到 ON
3. 保存
4. 前端 admin UI 应即刻收到确认 toast

**Fallback：CLI（仅在 admin UI 不可用时）：**

**v0.10 改临时脚本 `f_step71_ga_flip.sh`**（与 §7.3 Layer 2 同款风格）：

```bash
#!/bin/bash
# F.5 §7.1 — GA flip：cosyvoice_clone_general_availability_enabled=true + restart gateway
set -euo pipefail

# 1. 备份当前 admin_settings.json
cp /opt/aivideotrans/config/admin_settings.json \
   /opt/aivideotrans/backups/admin_settings_pre_ga_$(date +%Y%m%d_%H%M%S).json

# 2. jq flip flag
cd /opt/aivideotrans/config
jq '.cosyvoice_clone_general_availability_enabled = true' admin_settings.json > admin_settings.json.new
mv admin_settings.json.new admin_settings.json

# 3. compose restart gateway
cd /opt/aivideotrans
docker compose --env-file /opt/aivideotrans/config/.env restart gateway

echo "OK: GA flipped on; admin + allowlist + general users 全员可见 clone 按钮"
```

```powershell
& 'D:\daili\scripts\SCP-US-Via-154.cmd' 'D:\Claude\temp\f_step71_ga_flip.sh' '/tmp/f_step71_ga_flip.sh'
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'bash /tmp/f_step71_ga_flip.sh'
Remove-Item D:\Claude\temp\f_step71_ga_flip.sh
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'rm -f /tmp/f_step71_ga_flip.sh'
```

**验证（`f_step71_ga_flip_verify.sh`）：**

```bash
#!/bin/bash
# F.5 §7.1 — GA flip 后 clone-gate 返 general_availability_enabled=true
set -euo pipefail

DEPLOY_TS=$(cat /opt/aivideotrans/data/runtime_logs/current_phase42_deploy_ts)
DEPLOY_DIR=/opt/aivideotrans/data/runtime_logs/deploy_${DEPLOY_TS}

curl -s -b "$(cat ${DEPLOY_DIR}/admin_session.txt)" \
     https://aitrans.video/api/voice/cosyvoice/clone-gate \
  | jq '.general_availability_enabled'
# 期望：true
```

```powershell
& 'D:\daili\scripts\SCP-US-Via-154.cmd' 'D:\Claude\temp\f_step71_ga_flip_verify.sh' '/tmp/f_step71_ga_flip_verify.sh'
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'bash /tmp/f_step71_ga_flip_verify.sh'
Remove-Item D:\Claude\temp\f_step71_ga_flip_verify.sh
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'rm -f /tmp/f_step71_ga_flip_verify.sh'
```

### 7.2 普通用户黑盒验证

非 admin 测试账号登录：
- `GET /clone-gate` 现在应返 `can_access_clone=true`（`authorization_reason="general_availability"`）
- UI 应能看到「克隆音色」按钮

### 7.3 Rollback 开关（**三层，v0.2 字段修正**）

**Layer 1 — flip 关 GA**：admin UI（推荐）或 CLI 把 `cosyvoice_clone_general_availability_enabled=false` + 重启 gateway。生效 < 30 秒。
- **用途**：发现用户报错 / DashScope cost 失控 → 立即关 GA；admin + allowlist 用户仍可用
- **影响范围**：仅 GA 流；admin 烟测路径保留

**Layer 2 — 关全部 cosyvoice clone hard kill**：CLI / admin API 把 `cosyvoice_clone_worker_enabled=false` + 重启 gateway。
- **用途**：发现严重 bug（worker 死循环 / OSS 泄漏 / 误克隆）→ 全员立即不可用
- **效果**：clone-gate 返 `runtime_ready=false, runtime_unavailable_code="worker_disabled"`；admin 也不可用
- **v0.3 修正**：admin UI 目前**没有暴露**这个 toggle（`frontend-next/.../admin/settings/page.tsx` grep 确认：line 836-837 只有 `general_availability_enabled` checkbox，`cosyvoice_clone_worker_enabled` 仅作 save-through 字段无 UI 控件）。所以 Layer 2 **不能仅靠 admin UI**——必须用 CLI。

**`f_layer2_disable_clone.sh`**（v0.10 改临时脚本，Codex 九审 P1-4 —— 与 §9.2 Level 1 rollback 同级 hard-kill 操作必须走脚本）：

```bash
#!/bin/bash
# F.7 §7.3 Layer 2 hard-kill — cosyvoice_clone_worker_enabled=false + restart gateway
# 用途：发现严重 bug（worker 死循环 / OSS 泄漏 / 误克隆）时全员立即不可用
# **破坏性操作**：会让所有 cosyvoice clone 路径（含 admin）立即不可用
set -euo pipefail

DEPLOY_TS=$(cat /opt/aivideotrans/data/runtime_logs/current_phase42_deploy_ts 2>/dev/null || echo "manual_$(date +%Y%m%d_%H%M%S)")
BACKUP_DIR=/opt/aivideotrans/data/runtime_logs/deploy_${DEPLOY_TS}
mkdir -p ${BACKUP_DIR}

# 1) 备份当前 admin_settings.json（部署 log 目录 + backups 双备份）
cp /opt/aivideotrans/config/admin_settings.json \
   ${BACKUP_DIR}/admin_settings_pre_layer2.json
cp /opt/aivideotrans/config/admin_settings.json \
   /opt/aivideotrans/backups/admin_settings_pre_layer2_$(date +%Y%m%d_%H%M%S).json

# 2) jq flip worker_enabled=false
cd /opt/aivideotrans/config
jq '.cosyvoice_clone_worker_enabled = false' admin_settings.json > admin_settings.json.new
mv admin_settings.json.new admin_settings.json

# 3) restart gateway 让 Pydantic settings 重读
cd /opt/aivideotrans
docker compose --env-file /opt/aivideotrans/config/.env restart gateway

echo "OK: cosyvoice_clone_worker_enabled=false; clone path now disabled for ALL users (including admin)"
echo "Backup: ${BACKUP_DIR}/admin_settings_pre_layer2.json"
```

```powershell
& 'D:\daili\scripts\SCP-US-Via-154.cmd' 'D:\Claude\temp\f_layer2_disable_clone.sh' '/tmp/f_layer2_disable_clone.sh'
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'bash /tmp/f_layer2_disable_clone.sh'
Remove-Item D:\Claude\temp\f_layer2_disable_clone.sh
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'rm -f /tmp/f_layer2_disable_clone.sh'
```

**Layer 2 触发后立即跑 §5.1 Step 1 effective 校验**：确认 `cosyvoice_clone_worker_enabled=false` 真的生效，clone-gate `runtime_ready=false, runtime_unavailable_code="worker_disabled"`。

**更优解（不在 F 范围）**：F 之前或之后加 admin UI `worker_enabled` toggle 作为独立任务，让 hard-kill 可以一键 UI 操作。F 进行中先用 CLI。

**v0.1 错误纠正**：v0.1 写 `cosyvoice_clone_admin_only=false` 不对——这个字段不存在。**正确**是 `cosyvoice_clone_worker_enabled=false`

**Layer 3 — 镜像 rollback**（只在严重情况下）：见 §9.2 Level 1/2/3 剧本。

**三层选择树：**

```
发现问题
  ├─ 只影响 GA 流量 (admin 烟测仍正常) → Layer 1
  ├─ 影响所有 cosyvoice clone（含 admin）→ Layer 2
  └─ 影响范围超出 cosyvoice clone（其他端点也红）→ Layer 3
```

### 7.4 GA flip 不分阶段（**决策记录**）

Phase 4.2 plan v4 已定：stage 1 = admin-only（即 F.3-F.4），stage 2 = GA。**不**做 1% / 10% / 50% 滚动灰度。理由：

- 单 region 单 host，没有按流量分流的基础设施
- 付费 API 风险已经在 admin-only 阶段烟测验过
- 加阶段灰度会让 rollback 决策窗口模糊（"50% 时该不该回退？"）

GA flip = 一次 boolean flag 翻转 + 一次 gateway restart。

---

## §8 F.6 生产监控点（24h 观察）

### 8.1 监控对象

| 项 | 来源 | 异常阈值 | 处理 |
|---|---|---|---|
| **worker audit JSONL** | `/opt/aivideotrans/data/runtime_logs/audit_*.jsonl` | 任一 clone audit 缺 `worker_request_id` | 立即 Layer 2 rollback |
| **DashScope cost** | DashScope console（手动） | 日累计 > ¥50 且非预期 | Layer 1 关 GA |
| **Gateway error code** | `docker logs aivideotrans-gateway` 过滤 5xx | 5xx 占比 > 1% / 10 分钟窗口 | 查根因，必要时 Layer 1 |
| **user_voices 增长** | `SELECT count(*) FROM user_voices WHERE provider='cosyvoice_voice_clone' AND created_at > <ga_flip_at>` | 24h 后 > 100 行 → 观察是否符合预期 | 不一定异常，但确认是否有误触 |
| **武汉 worker 健康** | admin auth `/api/admin/mainland-voice-worker/healthz`（需 admin token，不是 `/gateway/health`）| ping fail / handshake 断 | 立即 Layer 2 + WG 排障 |
| **OSS 使用** | 阿里云 OSS console | 单日上传量 > 1 GB | 异常（每个 clone 样本 ≤ 10 MB，1 GB = 100+ clone）|
| **Admin 烟测 voice cleanup** | §6.8 SOP 完成确认 | T+24h 未跑 cleanup | 跑 §6.8 SOP；不影响 GA 流量但污染 admin voice library |

**v0.2 移除项（Codex P1 #4）：** ~~temp expiry sweeper 日志~~——sweeper 还未实现（grep 确认 `temporary_expires_at` 全仓只有 schema/ORM/test/spec），F 阶段不监控这项。Sweeper 实现 + cron 启动留独立专项。临时音色 row 在 F 期会**正常堆积**——清理依赖 §6.8 admin 手动 SOP（仅烟测 voice）+ 未来 sweeper 上线后批量清理（用户态 voice）。

**长观察期必须走 bind-mount runtime_logs/**（feedback_docker_logs_ephemeral.md 教训：docker json-file logs 随 container recreate 丢失）。

### 8.2 观察检查点

- **T+1h**：F.5 GA flip 后 1 小时；查所有上述 7 项
- **T+6h**：再查一遍，特别是 user_voices 增长 + DashScope cost
- **T+24h**：F.6 结束，写一份观察总结

### 8.3 异常上报

任何观察期发现 user-visible 问题 → 立即 Layer 1（关 GA）+ 写 incident report 落到 `docs/incidents/2026-05-27-phase42-go-live-<n>.md`。

---

## §9 F.7 明确禁止 / 失败回滚剧本

### 9.1 明确禁止清单

| 禁止 | 理由 |
|---|---|
| ❌ 没做 F.0 in-flight psql 检查 → 不许动 docker compose | 中断用户任务 |
| ❌ 没做 F.4 真烟测 → 不许 GA flip | 用户首次接触付费功能就坏体验 |
| ❌ 跳过 030 直接 apply 031 | alembic 链断 |
| ❌ `docker compose --env-file ... up -d` 整盘 recreate | 触发依赖 service 重启，中断 in-flight |
| ❌ 部署期间并发其他 PR merge / 部署 | 状态机难追踪 |
| ❌ admin 账号克隆出来的 voice 让用户复用 | admin 烟测产物应单独标记 / 24h 后删除 |
| ❌ DashScope key / OSS key 通过 `docker exec` echo 出来到日志 | 凭证泄漏 |
| ❌ F.5 GA flip 后忘记 §8 监控 | 没人盯付费 API 等于裸奔 |
| ❌ rollback 镜像后不回滚 alembic | code 030 兼容旧 schema 但运行行为不一致 |

### 9.2 失败回滚剧本（按严重度分级）

**Level 1 — 部署中任一健康检查红（v0.9 重写：临时脚本 + 持久 DEPLOY_TS，无 `<ts>` 占位符）：**

**`f_rollback_level1.sh`**：

```bash
#!/bin/bash
# F.7 §9.2 Level 1 — gateway healthz 红时撤回 app 树 + rebuild gateway
# **破坏性操作**：会把当前 /opt/aivideotrans/app 移到 app.failed.${DEPLOY_TS} 留证
set -euo pipefail

DEPLOY_TS=$(cat /opt/aivideotrans/data/runtime_logs/current_phase42_deploy_ts)
BACKUP_TGZ=/opt/aivideotrans/backups/app_pre_phase42_${DEPLOY_TS}.tar.gz

# 校验备份存在再动手
if [[ ! -f "${BACKUP_TGZ}" ]]; then
    echo "ERROR: ${BACKUP_TGZ} 不存在——F.0 §2.5 备份缺失，rollback 中止"
    exit 1
fi

cd /opt/aivideotrans

# 1) 把当前 app/ 改名留证（不删除，用 DEPLOY_TS 命名，可追溯）
mv app app.failed.${DEPLOY_TS}

# 2) 从备份恢复 pre-F 状态
tar xzf "${BACKUP_TGZ}" -C /opt/aivideotrans/

# 3) compose rebuild + recreate gateway
docker compose --env-file /opt/aivideotrans/config/.env build gateway
docker compose --env-file /opt/aivideotrans/config/.env up -d --no-deps --force-recreate gateway

echo "OK: rolled back to pre-F state; failed app tree preserved at /opt/aivideotrans/app.failed.${DEPLOY_TS}"
```

```powershell
& 'D:\daili\scripts\SCP-US-Via-154.cmd' 'D:\Claude\temp\f_rollback_level1.sh' '/tmp/f_rollback_level1.sh'
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'bash /tmp/f_rollback_level1.sh'
Remove-Item D:\Claude\temp\f_rollback_level1.sh
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'rm -f /tmp/f_rollback_level1.sh'
```

**v0.8 错误纠正：** v0.8 写 `app_pre_phase42_<ts>.tar.gz` —— `<ts>` 是占位符**不会被自动展开**；v0.9 改用持久 `${DEPLOY_TS}` 自动读取。

**Level 2 — F.4 烟测发现功能红（但 schema 已 apply）：**

- 不动 schema（"写入后不 downgrade"，§3.3 原则）
- 镜像层面 rollback（同 Level 1）
- 030/031 列就空着，code 旧版不读这些列，安全

**Level 3 — F.5 后用户报错：**

- 第一动作：**Layer 1 关 GA**（admin UI flip `general_availability_enabled=false` + compose restart gateway）
- 不动其它，先观察
- 若 admin 也复现 → Layer 2 关全部 cosyvoice clone（CLI flip `cosyvoice_clone_worker_enabled=false`，见 §7.3）

**Level 4 — 严重数据问题（误克隆 / 跨用户）：**

- **立即 Layer 2** 关全部 cosyvoice clone
- DB snapshot 立刻 dump 一份留证：`docker exec aivideotrans-postgres pg_dump -U avt -d aivideotrans | gzip > /opt/aivideotrans/backups/db_incident_$(date +%Y%m%d_%H%M%S).sql.gz`
- 评估 alembic downgrade 是否合适（§3.3 原则：**写入后原则上不 downgrade**，除非证明 schema 本身有 corruption）
- 写 incident report，**24h 内**给出根因 + 修复方向

**所有 Level 命令必须用根 compose + `--env-file`**，不允许：
- ❌ `docker restart aivideotrans-gateway`（绕过 compose 配置）
- ❌ `cd /opt/aivideotrans/gateway && docker compose ...`（错目录）
- ❌ `docker compose build gateway`（缺 `--env-file`）

---

## §10 F.8 开放问题（v0.2：v0.1 大部分已闭环）

**v0.1 7 个开放问题里，6 个在 v0.2 已闭环：**

| # | v0.1 问题 | v0.2 决策 |
|---|---|---|
| Q1 | 030/031 partial index 是否 CONCURRENTLY？ | **已闭环**：grep 确认非 CONCURRENTLY，普通 `op.create_index`。§3.1 加 row-count precheck（<10k 接受短锁，10k-100k 凌晨窗口，>100k 改 migration）|
| Q2 | app 容器继续 dev mode bind mount？ | **已闭环**：F 保留 bind mount 不动；切回不可变镜像留 Phase 5 / 后续专项（§4.5 明示） |
| Q3 | temp expiry sweeper 是否在 F 启动？ | **已闭环**：F **不**启动 sweeper（未实现）；admin 烟测 voice 走 §6.8 手动 SOP；用户态 voice 暂堆积，留独立专项 |
| Q4 | A0a fix 与 F.2 forward overlay？ | **已闭环**：§4.4 加 A0a checksum grep（部署后扫容器内 `sample_uploader.py` 不含 `ResponseContentType=` 生效行）|
| Q5 | 烟测 voice 24h 处理？ | **已闭环**：§6.8 admin 手动 cleanup SOP（DashScope delete + user_voices soft-delete）|
| Q7 | 是否需要 staging 主机？ | **已闭环**：当前无 staging；F.0-F.4 在 prod 上做 dry-run + admin 烟测就是 staging 替代。建立独立 staging 留更后阶段 |

**仅剩 1 个仍要 Codex / 用户拍板：**

**Q6 — 部署窗口具体时段**：
   - 跨境用户活跃时段：北美 evening / 国内 morning
   - 建议窗口：**北京时间凌晨 4-6 点**（北美下午 1-3 点）
   - **F spec 不硬性约束**；用户在准备 F 执行前最终拍板，写到 incident-ready 文件里

---

## §11 验收标准（DoD）

F 完成 = 以下全部满足：

- [ ] F.0 检查全 7 项绿（in-flight blocker 空 / alembic head=029 / 镜像 hash 记录 / **AVT_* env 10 项**完整 / app 容器 `client_factory` import sanity / `user_voices` row 数 < 10k / 备份 3 个产物）
- [ ] F.1 alembic head = 031；DB schema 与 ORM 一致；migration 耗时 < 30 秒
- [ ] F.2 三步部署（**根 compose + `--env-file`**）各 healthz 绿；A0a checksum 校验通过；前端可访问；记录每步的 git commit
- [ ] F.3 admin-only flag 黑盒验证通过（**正确字段** `cosyvoice_clone_worker_enabled=true` + `general_availability_enabled=false`；admin 可见 / 普通不可见）
- [ ] F.4 真烟测 6 项全绿 + 1 次真 clone（file）+ 1 次真 clone（segments）+ 真 TTS
- [ ] **§6.8 admin 烟测 voice 手动 cleanup SOP T+24h 内完成**（v0.2 新增）
- [ ] F.5 GA flip 完成（**admin UI 优先**）+ 普通用户黑盒可见
- [ ] F.6 监控 24h 无 anomaly；写监控总结
- [ ] 完整 incident-report-ready：所有命令、产物路径、healthz response 留档到 `runtime_logs/deploy_<ts>/`
- [ ] 三层 rollback 演练：Layer 1 (`general_availability_enabled` flip)、Layer 2 (`cosyvoice_clone_worker_enabled` flip)、Layer 3 镜像 rollback —— 在本地或 staging 走一遍 dry-run（若没有 staging，至少在 F.0 之前用 docker compose 本地 mock 走完 Layer 1/2）

---

## §12 触发实施

Spec 通过 Codex 复审后：

1. 修复 spec 反馈（如有）
2. 用户选窗口（建议北京时间凌晨）
3. 准备 admin session cookie / 测试音频 / 测试段
4. 严格按 F.0 → F.7 顺序执行
5. **不**部署 SG / 其它 region
6. F 结束 = Phase 4.2 整体收口

---

## §13 时间估算

| 阶段 | 预估 |
|---|---|
| F.0 检查 + 备份 | 15 分钟 |
| F.1 alembic 030+031 | 5 分钟 |
| F.2 三步部署 | 15 分钟（含 next.js build） |
| F.3 admin-only 验证 | 10 分钟 |
| F.4 admin 真烟测（含 1 次真 clone）| 30 分钟 |
| 间隔观察 | 30 分钟 |
| F.5 GA flip | 5 分钟 |
| F.6 监控 | 24h 后台 |
| **窗口期 P0-P5** | **~110 分钟（1h50m）+ 24h 监控** |

---

## §14 v0.1 → v0.2 修订记录

**Codex 一审（PR #16 后 F spec 复审）4 项 P1 + 4 项 P2 全部应用：**

### P1（事实层错误，必修）

| # | v0.1 问题 | v0.2 修复 | 落点 |
|---|---|---|---|
| P1-1 | 部署命令绕过 compose / 手工铺 next-standalone / 缺 `--env-file` | 全部命令用 `cd /opt/aivideotrans && docker compose --env-file /opt/aivideotrans/config/.env <cmd>`；next 用 compose `build next` + `up -d next`；app sync 用 rsync 整树覆盖 | §4.2 / §4.3 / §4.4 / §4.5 / §4.6 |
| P1-2 | env 名错（用 `MAINLAND_VOICE_*` / `ALIYUN_OSS_*` / `DASHSCOPE_API_KEY` 列在 US gateway） | 改为 `AVT_*` prefix；`DASHSCOPE_API_KEY` 移到武汉 worker 端不在 US 查；OSS 4 项（无 REGION）；admin_settings 字段改为 `cosyvoice_clone_worker_enabled` / `cosyvoice_clone_user_allowlist` / `cosyvoice_clone_general_availability_enabled`；Layer 2 hard kill 用 `cosyvoice_clone_worker_enabled=false` | §2.4 / §5.1 / §7.3 |
| P1-3 | 030/031 partial index 创建方式留 open，rollback 太宽松 | grep 确认非 CONCURRENTLY；§3.1 加 `SELECT count(*) FROM user_voices` precheck + 阈值（<10k / 10k-100k / >100k）；§3.3 rollback 收紧 "F.4 写入后原则不 downgrade，优先 flag/code rollback" | §3.1 / §3.3 |
| P1-4 | sweeper 作为 F 监控/DoD（实际未实现）| 移除 §8 sweeper 监控项；§6.8 新增 admin 烟测 voice 手动 cleanup SOP（DashScope delete + user_voices soft-delete） | §6.8 / §8 / §11 |

### P2（建议层，全部接受）

| # | v0.1 问题 | v0.2 修复 | 落点 |
|---|---|---|---|
| P2-1 | in-flight 阻断范围太宽（含 editing）| 拆分 blocker（`running/pending/awaiting_review`）vs awareness（`editing/paused`），两个独立 SQL | §2.1 |
| P2-2 | GA flip 用 CLI jq 主路径 | 改 admin UI 优先（经 StrictBool / full-body save 测试覆盖），CLI 作 fallback | §7.1 |
| P2-3 | healthz 写未验证的 `/admin/healthz` | grep 确认实际是 `/gateway/health`（app 层）；worker healthz 在 `/api/admin/mainland-voice-worker/healthz` 走 admin auth；§4.4 改用 `/gateway/health`；§8 worker 监控改用 admin-auth endpoint | §4.4 / §8 |
| P2-4 | A0a overlay 没有 checksum 校验 | §4.4 加 grep 检查容器内 `sample_uploader.py` 不含 `ResponseContentType=` 生效行 | §4.4 |

### v0.2 额外补充（自查）

- §0.4 加 "v0.2 sanity 已 grep 确认" 表 —— 11 条事实全部带代码出处，避免再凭推断写
- §11 DoD 6/9/11 项更新对齐 v0.2 修订（env 10 项、admin_settings 正确字段、§6.8 cleanup、三层 rollback）

## §15 v0.2 → v0.3 修订记录（Codex 二审 7 项 P1 + 4 项 P2 全部应用）

### P1（事实层错误，必修）

| # | v0.2 错误 | v0.3 修复 | 落点 |
|---|---|---|---|
| P1-1 | `/gateway/health` 期望返 worker/uploader 字段 | grep 确认只返 `{status, auth_required}`；worker 用 `/api/admin/mainland-voice-worker/healthz`；uploader 用容器内 `build_sample_uploader_from_settings(settings)` 探活；admin endpoint 路径 `mainland-voice-worker` 不是 `cosyvoice-worker` | §4.4 / §6.2 / §6.3 |
| P1-2 | `get_uploader()` / `u.healthz()` / `u.delete(key)` 是虚构 | 改为 `build_sample_uploader_from_settings(settings)` 工厂 + `delete_uploaded_url(url)` 真接口；A0a grep 路径 `/app/gateway/` → `/opt/gateway/`（gateway 容器 WORKDIR）| §4.4 / §6.2 / §6.8 |
| P1-3 | `scripts/worker_smoke.py` 不存在 | 全仓 grep 确认只有 `pan_backup_smoke.py`；§6.3 改调既有 admin healthz endpoint；未来补强写成独立专项 | §6.3 |
| P1-4 | `pg_dump -U aivideotrans` 用户名错 | docker-compose 实际 `POSTGRES_USER: avt`；改 `pg_dump -U avt -d aivideotrans` | §2.5 / §9.2 Level 4 |
| P1-5 | cleanup SOP 用虚构 `/admin/delete_voice` + `X-Worker-Auth` | grep 确认 worker DELETE 是 `/cosyvoice/voices/{voice_id}` HMAC；client wrapper 是 `MainlandWorkerClient.delete_voice(voice_id, WorkerDeleteVoiceRequest(...))`；§6.8 重写用 `build_client_from_env()` + 真实接口 | §6.8 |
| P1-6 | smoke SQL 期望 `source_type='file_upload'` | grep 030 migration 确认**不含** `source_type` 列；改用 `clone_sample_segment_ids IS NULL`（file）vs 非空（segments）作 distinguishing marker | §6.4 / §6.6 |
| P1-7 | §9.2 rollback 残留 `mv gateway gateway.failed...` + 缺 `--env-file` | 改为：F.0 §2.5 增加 app 树 tar 备份；rollback 用 `tar xzf <backup>` 恢复 + 根 compose `--env-file` rebuild/recreate；禁止 `docker restart` 绕过 compose | §2.5 / §9.2 |

### P2（建议层，全部接受）

| # | v0.2 问题 | v0.3 修复 | 落点 |
|---|---|---|---|
| P2-1 | §2.3 `next-standalone BUILD_ID` 残留（与 compose `next` 设计不符）| 改为 `docker inspect aivideotrans-next` 拉 BuildLabel / Status / Health | §2.3 |
| P2-2 | §4.3 `diff -r app/ staging/` 不可靠 | 改为 git SHA 比对 + 6 个关键文件 sha256 cross-host check（注：`git archive` 不带 `.git`，需要单独 SCP `.git_archive_commit`）| §4.3 |
| P2-3 | A.2a 路径写 `gateway/_audio_assembly.py` | 实际是 `gateway/audio_assembly.py`（无下划线前缀）| §0.1 |
| P2-4 | Layer 2 hard kill 假设 admin UI 可一键 flip `worker_enabled` | grep `frontend-next/.../admin/settings/page.tsx` 确认 UI 只暴露 `general_availability_enabled` checkbox；`worker_enabled` 仅 save-through 无 UI 控件。Layer 2 在 F 阶段**必须**走 CLI；UI toggle 留独立专项加 | §7.3 |

### v0.3 额外补充

- §0.4 sanity 表扩到 14 条事实，全部带代码出处 + 显式标注"v0.1/v0.2 错误"
- §2.6 user_voices 行数 precheck 单独抽出小节（§3.1 引用）
- §9.2 所有 Level 命令统一用根 compose + `--env-file`，明示三个禁止模式

## §15.5 v0.3 → v0.4 修订记录（Codex 三审 4 项 P1 + 3 项 P2）

### P1（事实层错误，必修）

| # | v0.3 错误 | v0.4 修复 | 落点 |
|---|---|---|---|
| P1-1 | §4.4 / §6.2 / §6.8 import 写 `from gateway.cosyvoice_clone.X` | grep 确认 gateway container WORKDIR=/opt/gateway，包路径无 `gateway.` 前缀；3 处全改为 `from cosyvoice_clone.X import Y` | §4.4 / §6.2 / §6.8 |
| P1-2 | cleanup import 写 `from src.services.mainland_worker.client import build_client_from_env` | grep 确认实际是 `client_factory.py:93`；v0.4 改为：在 `aivideotrans-app` 容器跑（WORKDIR=/opt/aivideotrans/app，PYTHONPATH 天然含 src/），import `from src.services.mainland_worker.client_factory import build_client_from_env` | §6.8 |
| P1-3 | §8 监控表写 `/api/admin/voice/cosyvoice-worker/healthz`；§14 P2-3 同样错 | 全文统一为 `/api/admin/mainland-voice-worker/healthz`（§0.4 sanity 表已对，§8 + §14 同步修正）| §8 / §14 |
| P1-4 | §2.4 env 用 `grep ... \| wc -l` 不验空值 | 改为 python 显式 missing + empty + EXPECT_EQ 三段校验，退出码 0 + 'OK' 才通过 | §2.4 |

### P2（建议层，全部接受）

| # | v0.3 问题 | v0.4 修复 | 落点 |
|---|---|---|---|
| P2-1 | §0.1 把 `clone_sample_seconds` / `clone_sample_segment_ids` / `source_type` 归到 030 | grep `028_user_voice_source_metadata.py:27/41/44` 确认这三列已上线（来自 028，不是 030）；§0.1 改为只列 030 的 routing/billing 列；§0.4 加 028 列表 row 标注 |  §0.1 / §0.4 |
| P2-2 | runtime_logs deploy_<ts> 目录没显式 mkdir | §2.0 新增 "F.0 第一步" 创建目录；所有后续步骤的 log/output 落进去 | §2.0 |
| P2-3 | F spec 还是 untracked 文档，建议单独 commit | v0.4 spec 全绿后单独 commit（不混在 F 实施里），main 与正在执行的 spec 完全一致 | 提交流程说明（本节） |

### 关于 P2-3 的执行决定

v0.4 spec 一旦 Codex 签可执行：
1. 单独 commit spec 文件到 main（commit message 含 v0.4 决议），**不混在 F.0 实施提交里**
2. push 到 origin/main
3. 然后才进入 F.0

这样部署执行者看到的 spec 就是 main HEAD 的版本，不会 spec 漂移。

## §15.6 v0.4 → v0.5 修订记录（Codex 四审 3 项 P1 + 3 项 P2）

### P1（事实层错误，必修）

| # | v0.4 错误 | v0.5 修复 | 落点 |
|---|---|---|---|
| P1-1 | §5.1 用 raw `cat admin_settings.json \| jq` 看新字段会误判（缺字段 → null）；没有 `worker_enabled=false` 的 init 流程 | grep 确认 `admin_settings.py:196` 默认 `worker_enabled: bool = False`；改用 `GET /api/admin/settings` 看 effective 值；`worker_enabled != true` 时给 Step 2 init 流程（POST 写或 jq fallback + restart）；allowlist 可空——admin 走 role 分支 | §5.1 |
| P1-2 | §6.4 smoke SQL 期望写错：`worker_provider` 允许 `'cosyvoice_voice_clone'`、`billing_sku` 含 `cosyvoice-v3.5-flash` | grep `api.py:970-998` 真实 INSERT：`worker_provider='cosyvoice'`（**只**这一个值）；`billing_sku=None` 首次烟测必 NULL（`api.py:994-995` 注释明确）；spec 改 18 字段精确断言表，每条带代码出处 | §6.4 |
| P1-3 | §6.7 checklist 写 "OSS healthz ok" | uploader 没有 `healthz` 方法；改为 "OSS uploader factory/config probe ok"；端到端 OSS round-trip 留 F.6 或独立专项 | §6.7 / §11 |

### P2（建议层，全部接受）

| # | v0.4 问题 | v0.5 修复 | 落点 |
|---|---|---|---|
| P2-1 | 命令混用本地 bash 语法和 Windows `.cmd` | §2.0.0 新增"命令风格约定"小节：远端 bash 命令用 `D:\daili\scripts\SSH-US-Via-154.cmd "..."` 包装；推文件用 `Deploy-US-Via-154.cmd` / `SCP-US-Via-154.cmd`；禁止手工直连 5.78.122.220 绕过 154 代理 | §2.0.0 |
| P2-2 | §6.1/§6.3 多处用 admin session cookie 但没说怎么获取 | §2.0.0 末尾新增 "Admin session cookie 准备"：浏览器登录 → F12 → 复制 `avt_session` 到 `runtime_logs/deploy_${TS}/admin_session.txt`（chmod 600）；后续 curl 用 `-b "$(cat .../admin_session.txt)"`；F.6 结束删除 | §2.0.0 |
| P2-3 | v0.4 spec 仍 untracked | v0.5 全绿 → 单独 commit + push spec 到 main（已在 §15.5 P2-3 写流程），然后才 F.0 | §15.5 |

### 关键差异：v0.4 → v0.5 字段语义对齐

v0.4 / v0.3 部分内容是对的，但 §6.4 smoke SQL 的 worker_provider / billing_sku 期望值是凭推断写的——这是 v0.5 最关键的 P1 修：

```
v0.4 spec 写            真实 api.py 写            修正后
worker_provider:        WORKER_PROVIDER_COSYVOICE  worker_provider = 'cosyvoice'
  'cosyvoice_voice_clone'  = 'cosyvoice'
  或 'cosyvoice'

billing_sku:           billing_sku=None          billing_sku IS NULL
  含 cosyvoice-v3.5-flash  (永久 None 直到首次实账单回填)
```

若 v0.4 直接执行：F.4 烟测 SQL 100% 在 `worker_provider` / `billing_sku` 两条断言失败 → 看起来像"代码 bug"但实际是 spec 错——这是必须 v0.5 修的核心原因。

## §15.7 v0.5 → v0.6 修订记录（Codex 五审 1 项 P1 + 2 项 P2）

### P1（危险 bug 风险，必修）

| # | v0.5 错误 | grep 出处 | v0.6 修复 |
|---|---|---|---|
| P1-1 | §5.1 Step 2 用 `POST /api/admin/settings -d '{"cosyvoice_clone_worker_enabled": true}'` partial body 初始化 worker_enabled | `gateway/admin_settings.py:350-377` endpoint docstring 明文 "FULL BODY SEMANTICS"——partial body 会让 Pydantic 把所有未传字段重置为默认值；回归测试 `test_post_settings_with_missing_phase3_fields_resets_them_to_defaults` 锁了这条契约。还挂 `require_same_origin_state_change` CSRF，无 Origin header 直接 403 | §5.1 Step 2 重写：**CLI JSON patch + restart** 作为唯一推荐路径（jq + mv + compose restart 三步）；若坚持 API 必须 GET full → modify → POST full body + `Origin: https://aitrans.video` header（标注为"复杂度远超 CLI，不推荐"备选记录）；明示 partial body POST 为禁止做法 |

### P2（建议层，全部接受）

| # | v0.5 问题 | v0.6 修复 | 落点 |
|---|---|---|---|
| P2-1 | §2.0.0 "复制 `avt_session` cookie 完整字符串"歧义 | 明确写 "**只复制 cookie name=value**" 格式：`avt_session=<value>`；标注**不要**复制 DevTools 表格里的 Domain/Path/Expires 等元数据列；给正反两个范例；文件内容就一行 | §2.0.0 |
| P2-2 | v0.5 spec 仍 untracked | v0.6 全绿 → 单独 commit + push spec 到 main（流程不变，见 §15.5 P2-3）| §15.5 / §15.6 / §15.7 一致 |

### 为什么 partial body POST 这条会让 F 直接失败

v0.5 写法的两条失败路径都很致命：

**路径 A（更可能）：** curl 没带 `Origin: https://aitrans.video` header → CSRF middleware 直接 403 `csrf_origin_rejected` → F.3 init 步骤无法完成 → admin 烟测做不起来 → F 卡死

**路径 B（更糟，如果绕过 A）：** 假设运维 ad-hoc 加 Origin 后 curl 通了 → endpoint full-body save 把请求 body 当**完整** AdminSettings → 缺失字段 Pydantic 默认填充 → 写回的 JSON 把生产已有的 `smart_auto_clone_enabled` / `smart_reuse_user_voice_enabled` / `smart_pause_on_possible_user_voice_match` / `pricing_*` / `review_prompts` 等数十个字段**静默重置为默认**

路径 B 的修复成本远超 F 本身：需要从备份恢复 admin_settings.json + 重启 + 对所有受影响功能回归测试。这就是为什么 v0.6 必须把 partial body POST 从 spec 里彻底删掉，**只**保留 CLI JSON patch 作为推荐路径。

## §15.8 v0.6 → v0.7 修订记录（Codex 六审 2 项 P1 + 1 项 P2）

### P1（执行级硬错，必修）

| # | v0.6 错误 | grep 出处 | v0.7 修复 |
|---|---|---|---|
| P1-1 | §5.1 Step 1 jq 读错层级 —— 直接 `jq '{ cosyvoice_clone_... }'` 在顶层取字段 | `gateway/admin_settings.py:347` `return {"settings": load_settings().model_dump()}` —— 顶层是 `{"settings": {...}}` 包裹层；顶层取这些字段全是 null | §5.1 Step 1 改为 `jq '.settings \| {...}'` 先进 `.settings` 子对象再字段筛选；§5.1 Step 2 (c) API 备选路径已经是 `.settings \| ...` 写对了 |
| P1-2 | `TS=$(date ...)` 在本地 shell 设置，后续 `SSH-US-Via-154.cmd "..."` 每条新远端 shell 看不到 `$TS` | 每次 SSH 调用独立 shell；`${TS}` 在远端空 → 路径解析成 `deploy_/...` | §2.0 重写：远端落 timestamp 文件 `/opt/aivideotrans/data/runtime_logs/current_phase42_deploy_ts`；后续所有远端命令开头**必须**含 `DEPLOY_TS=$(cat ...)` + `DEPLOY_DIR=...`；§2.5 备份脚本改单次 SSH 内 `&&` 连缀；§5.1 Step 2 / API 备选所有远端命令同款改写 |

### P2（建议层）

| # | v0.6 问题 | v0.7 修复 | 落点 |
|---|---|---|---|
| P2-1 | cookie 文件落远端缺具体写法 | §2.0.0 cookie 部分加：本地编辑器（不是 echo）创建 `D:\Claude\temp\admin_session.txt`（feedback_temp_files.md 约定）→ `SCP-US-Via-154.cmd` 推上去 → 远端 `chmod 600` → 本地立即 del；spec / shell 历史 / SSH 命令都不会带 cookie | §2.0.0 |

### v0.7 关键 invariant（v0.8 已废弃此写法）

v0.7 这条 invariant 自己就是错的（Codex 七审 P1-1 抓出）：

```
# ❌ v0.7 写法（PowerShell 双引号 + \$(...) 转义）—— 整链路无效
D:\daili\scripts\SSH-US-Via-154.cmd "DEPLOY_TS=\$(cat ...) && ..."
```

`\` 在 PowerShell 不是 `$()` 的转义符（PowerShell 用 backtick `` ` ``），传到 bash 时 `\$(...)` **阻止**了 bash 命令替换。v0.8 改用 PowerShell 单引号字符串：

```powershell
# ✅ v0.8 唯一可执行格式
& 'D:\daili\scripts\SSH-US-Via-154.cmd' 'DEPLOY_TS=$(cat /opt/aivideotrans/data/runtime_logs/current_phase42_deploy_ts) && DEPLOY_DIR=/opt/aivideotrans/data/runtime_logs/deploy_${DEPLOY_TS} && <bash command using ${DEPLOY_DIR}>'
```

PowerShell 单引号字符串**完全不展开**任何变量 / `$` / 反斜杠，cmd.exe → ssh → 远端 bash 看到的就是原汁原味的 `$(cat ...)`。复杂命令（含单引号 / heredoc / python -c）走临时脚本格式（§2.0.0）。

## §15.9 v0.7 → v0.8 修订记录（Codex 七审 2 项 P1 + 2 项 P2）

### P1（执行级硬错，必修）

| # | v0.7 错误 | grep 出处 | v0.8 修复 |
|---|---|---|---|
| P1-1 | 全文 `\$(cat ...)` 反斜杠转义方案在 PowerShell→cmd→bash 链路无效（PowerShell 不识别 `\`，bash 看到 `\$()` 反而不求值）| n/a（spec 自身约定错） | §2.0.0 重写"命令风格约定"为 PowerShell 单引号格式（`& '...' '...'`）；复杂命令（含单引号 / heredoc）走临时脚本模式；全文 5 处主要 ssh-via-154 调用全改 |
| P1-2 | 全文遗留 23+ 处裸 `ssh "..."` 简写——执行时会被照字面拷贝、绕过 154 SOCKS5 代理 | spec 自身 grep | Python 全文批量 sweep：所有 `ssh "..."` / `D:\daili\scripts\SSH-US-Via-154.cmd "..."` (双引号+ `\$`) 转换为 `& 'D:\daili\scripts\SSH-US-Via-154.cmd' '...'` 单引号 PowerShell 调用；含单引号的命令标注"建议改临时脚本"|

### P2（建议层）

| # | v0.7 问题 | v0.8 修复 | 落点 |
|---|---|---|---|
| P2-1 | cookie 上传步骤靠"手抄 timestamp"构造远端路径 | §2.0.0 cookie 部分改用 PowerShell 自动读：`$DEPLOY_TS = (& 'SSH-US-Via-154.cmd' 'cat ...').Trim(); $DEPLOY_DIR = "/opt/.../deploy_${DEPLOY_TS}"`；后续 SCP / chmod 直接用 `$DEPLOY_DIR`；步骤 3/4 显式说明为什么这里允许用 PowerShell 双引号（路径不含 `$(...)` 命令替换）| §2.0.0 cookie |
| P2-2 | §16 待审点列表残留"gateway 容器 import `src.services...client`"等已解决问题 | §16 已清掉旧问题（v0.6 已迁到 app 容器 + client_factory，v0.5 OSS uploader 已用真实 factory）；新建 v0.8 待审点 | §16 |

### v0.8 关键 invariant（取代 v0.7）

```powershell
# 简单命令（无内嵌单引号）：
& 'D:\daili\scripts\SSH-US-Via-154.cmd' '<full bash command verbatim>'

# 复杂命令（含单引号 / heredoc / python -c '...'）：
# 1) D:\Claude\temp\step_NN_<desc>.sh ← 编辑器手写
# 2) & 'D:\daili\scripts\SCP-US-Via-154.cmd' 'D:\Claude\temp\step_NN_<desc>.sh' '/tmp/step_NN_<desc>.sh'
# 3) & 'D:\daili\scripts\SSH-US-Via-154.cmd' 'bash /tmp/step_NN_<desc>.sh'
# 4) Remove-Item D:\Claude\temp\step_NN_<desc>.sh
#    & 'D:\daili\scripts\SSH-US-Via-154.cmd' 'rm -f /tmp/step_NN_<desc>.sh'
```

**PowerShell 单引号语义关键点**：单引号字符串 → 字面字符串、**不展开**任何 `$` / `${}` / `$(...)`；cmd.exe 透传 → ssh 透传 → 远端 bash 解析。所以 `'$(cat ...)'` 在 PowerShell 是字面 7 字符字符串，到 bash 才求值。

## §15.10 v0.8 → v0.9 修订记录（Codex 八审 3 项 P1 + 1 项 P2）

### P1（runbook 可执行性，必修）

| # | v0.8 错误 | grep 出处 | v0.9 修复 |
|---|---|---|---|
| P1-1 | 多个块标注"建议改临时脚本"但保留 inline 活命令（§2.4 env audit / §5.1 Step 1 / §5.1 Step 2 / §6.1 / §6.2 / §6.3 / §7.1 验证）| spec 自身 grep | **5 个步骤**全部改为完整临时脚本 + PowerShell SCP+exec+cleanup 模板：`f_step24_env_audit.py`、`f_step51_admin_settings_check.sh`、`f_step51_worker_enable.sh`、`f_step44_worker_probe.sh`、`f_step61_clone_gate.sh`、`f_step62_oss_uploader_probe.sh`、`f_step63_worker_healthz.sh`、`f_step71_ga_flip_verify.sh`。§5.1 API 备选路径标为"伪代码，不可复制" |
| P1-2 | 活命令仍用 `<admin_session_cookie>` / `$ADMIN_SESSION` 占位 | spec 自身 grep（4 处：§4.4 worker probe / §6.1 clone-gate / §6.3 worker healthz / §7.1 GA flip verify）| 所有 cookie 读取统一改成临时脚本内：`curl -s -b "$(cat ${DEPLOY_DIR}/admin_session.txt)" ...`，配合 §2.0.0 cookie 上传 SOP（PowerShell 自动读 timestamp + SCP），全无 `<placeholder>` |
| P1-3 | §9.2 Level 1 rollback 用 `<ts>` 占位 + 手工 `mv app app.failed.$(date +%s)` | spec 自身（line 1425）| 改完整 `f_rollback_level1.sh` 临时脚本：读持久 DEPLOY_TS → 校验备份存在 → `mv app app.failed.${DEPLOY_TS}` 留证 → `tar xzf ...${DEPLOY_TS}.tar.gz` 恢复 → compose rebuild gateway；破坏性操作显式标注 |

### P2（建议层）

| # | v0.8 问题 | v0.9 修复 | 落点 |
|---|---|---|---|
| P2-1 | code fence 语言混乱（PowerShell 命令在 ```bash 块）| 临时脚本一律 ```bash（脚本内容是 bash）；PowerShell wrapper 一律 ```powershell；v0.9 新加的 8 个临时脚本块都按此约定 | §2.4 / §2.5 / §5.1 / §6.1-6.3 / §7.1 / §9.2 全部 |

### v0.9 临时脚本清单（共 8 个 / 9 段，全部走 §2.0.0 复杂命令模式）

| 文件 | 位置 | 内容 |
|---|---|---|
| `f_step24_env_audit.py` | §2.4 | python 校验 10 个 AVT_* env 非空 + EXPECT_EQ |
| `f_step25_backup.sh` | §2.5 | DB / admin_settings / docker-compose / app 树 4 项备份 |
| `f_step44_worker_probe.sh` | §4.4 | gateway rebuild 后的 admin worker healthz probe |
| `f_step51_admin_settings_check.sh` | §5.1 Step 1 | 读 effective admin_settings 三字段 |
| `f_step51_worker_enable.sh` | §5.1 Step 2 | 备份 + jq 写 worker_enabled=true / GA=false + compose restart |
| `f_step61_clone_gate.sh` | §6.1 | admin clone-gate API smoke |
| `f_step62_oss_uploader_probe.sh` | §6.2 | gateway 容器内 build_sample_uploader_from_settings 探活 |
| `f_step63_worker_healthz.sh` | §6.3 | 武汉 worker admin healthz |
| `f_step71_ga_flip_verify.sh` | §7.1 | GA flip 后 clone-gate.general_availability_enabled=true |
| `f_rollback_level1.sh` | §9.2 Level 1 | rollback：mv app + tar restore + compose rebuild |

### 关键 invariant 强化

所有可执行命令现在都遵循 §2.0.0 两种格式之一：
- **简单**（无单引号嵌套）：PowerShell 单引号 `& 'D:\daili\...' '...'`
- **复杂**（含单引号 / heredoc / python -c / 多步骤）：临时脚本 SCP + exec + cleanup 四步

spec 中**绝无**剩下的"伪代码可复制"歧义；伪代码段（§5.1 API 备选）已明确标注且**不**给完整可执行命令。

## §15.11 v0.9 → v0.10 修订记录（Codex 九审 4 项 P1）

| # | v0.9 错误 | v0.10 修复 |
|---|---|---|
| P1-1 | §2.0.0 cookie chmod 用 PowerShell 双引号 `"chmod 600 ${DEPLOY_DIR}/..."`，依赖本地 `$DEPLOY_DIR`，与"远端命令读持久 DEPLOY_TS"规则不一致 | 改单引号 + 远端 cat 读：`'DEPLOY_TS=$(cat ...) && DEPLOY_DIR=... && chmod 600 ${DEPLOY_DIR}/admin_session.txt'`。**SCP 目标路径**仍允许 PowerShell 双引号（cmd 级参数不进 bash），明确区分 |
| P1-2 | §4.4 / §6.8 (×2) 仍有 inline `python -c '...'` 多行单引号 | 改 3 个 python 临时脚本：`f_step44_uploader_probe.sh`（合并到 §6.2 同款）、`f_step68_worker_delete_voice.py`（app 容器 + `docker cp`）、`f_step68_oss_delete.py`（gateway 容器 + `docker cp`）；同时 §4.4 A0a checksum grep 改 `f_step44_a0a_checksum.sh` 临时脚本 |
| P1-3 | §4.4/4.5/4.6 healthcheck `for i in 1..6; do ... \\\n done` 反斜杠换行，复制时易丢 | 3 处 loop 全改单行：`'for i in 1 2 3 4 5 6; do curl ... && break \|\| sleep 5; done'` |
| P1-4 | §7.3 Layer 2 hard-kill 是复合 inline 命令（备份 + jq + mv + compose restart）| 改 `f_layer2_disable_clone.sh` 临时脚本：DEPLOY_TS 持久读取 + 双备份（log 目录 + backups/）+ jq flip + compose restart + 成功 echo；与 §9.2 Level 1 rollback 同级处理 |

### v0.10 新增临时脚本

| 文件 | 落点 |
|---|---|
| `f_step44_a0a_checksum.sh` | §4.4 — A0a sample_uploader.py 不含 ResponseContentType= 生效行 |
| `f_step64_clone_logs.sh` | §6.4 c — gateway 日志 + audit JSONL grep |
| `f_step65_tts_routing_logs.sh` | §6.5 a — pipeline 日志 grep |
| `f_step68_worker_delete_voice.py` | §6.8 Step 2 — admin 烟测产物 worker DELETE |
| `f_step68_oss_delete.py` | §6.8 Step 4 — admin 烟测产物 OSS 删除 |
| `f_layer2_disable_clone.sh` | §7.3 Layer 2 — hard-kill cosyvoice clone |

v0.9 已有的 10 个 + v0.10 新增 6 个 = **共 16 个临时脚本**。所有复杂命令 / 高风险 rollback / cookie 操作都已脚本化，runbook 不再依赖执行者临场判断。

### v0.12 后占位符状态

- §6.8 cleanup 主路径不再含 `<DashScope voice_id>` / `<admin uuid>` / `<oss_url_from_audit>` 这类手工替换占位；所有 delete / soft-delete / OSS cleanup / verify 脚本都读 §6.7 生成的 `cleanup_inputs.json`。
- §6.5 `f_step65_smoke_job_id_save.sh` 的 `SMOKE_JOB_ID="<smoke_job_id>"` 是 runbook 中**唯一**需要人工填一次的数据入口；下游全部读 `${DEPLOY_DIR}/smoke_job_id.txt`。
- SQL 查阅模板里的 `<admin uuid>` 均标 `TEMPLATE ONLY - DO NOT RUN`；属于调试说明，不是执行路径。
- narrative `<job_id>` / `<n>` / URL 示例仅用于说明，不是命令。

## §15.12 v0.10 → v0.11 修订记录（Codex 十审 2 P1 + 1 P2）

### P1（执行级，必修）

| # | v0.10 错误 | v0.11 修复 |
|---|---|---|
| P1-1 | cleanup 脚本仍需手工编辑 `VOICE_ID = "<DashScope voice_id from §6.4>"`、`ADMIN_USER_UUID = "<admin uuid>"`、`OSS_URL = "<oss_url_from_audit>"` 三处占位 | §6.7 新增 `f_step67_collect_cleanup_inputs.sh`：从 user_voices 表 + audit JSONL 自动导出 `cleanup_inputs.json` 到 `${DEPLOY_DIR}/`，含 `admin_user_id` + `voices[]`（user_voice_id / voice_id / target_model / clone_sample_segment_ids / created_at）+ `oss_urls[]`。下游 4 个 cleanup 脚本（worker delete / soft delete / OSS delete / verify）全部读 JSON，**无人工占位编辑** |
| P1-2 | `ls -la /opt/aivideotrans/app/jobs/<job_id>/...` 活命令带 `<job_id>` 占位 | §6.5 新增 `f_step65_smoke_job_id_save.sh`：admin 烟测开始时**一次性**把 job_id 落到 `${DEPLOY_DIR}/smoke_job_id.txt`。下游 `f_step65_tts_segments_ls.sh` 读文件，不再占位。`smoke_job_id_save.sh` 是**唯一**需要手工填一次数据（admin 烟测的 job_id）的脚本，所有下游命令自动展开 |

### P2（建议层）

| # | v0.10 问题 | v0.11 修复 |
|---|---|---|
| P2-1 | §6.4 / §6.6 / §6.8 SQL 模板含 `<admin uuid>` 占位看起来像活命令 | 全部加 `-- TEMPLATE ONLY - DO NOT RUN: 替换为 §6.7 cleanup_inputs.json.admin_user_id` 注释；明确这些是查阅模板，不是 runbook 执行路径 |

### v0.11 新增临时脚本

| 文件 | 落点 | 输入来源 |
|---|---|---|
| `f_step65_smoke_job_id_save.sh` | §6.5 — 保存 smoke job_id | **唯一**人工填一次 |
| `f_step65_tts_segments_ls.sh` | §6.5 — 列 TTS 产物 | 读 smoke_job_id.txt |
| `f_step67_collect_cleanup_inputs.sh` | §6.8 Step 1 — 收集 cleanup 全部 inputs | 自动从 DB + audit JSONL |
| `f_step68_soft_delete.sh` | §6.8 Step 3 — user_voices soft-delete | 读 cleanup_inputs.json |
| `f_step68_verify.sh` | §6.8 — cleanup 完成校验 | 读 cleanup_inputs.json |

并修改：
- `f_step68_worker_delete_voice.py`：循环 `cleanup_inputs.json.voices[]`
- `f_step68_oss_delete.py`：循环 `cleanup_inputs.json.oss_urls[]`

### 残留占位符 audit

**所有可执行临时脚本**：0 处占位符（除 §6.5 smoke_job_id 唯一入口点）

**模板示意 SQL（marked DO NOT RUN）**：5 处 `<admin uuid>` —— 全部带 `TEMPLATE ONLY - DO NOT RUN` 注释；属查阅 / 调试用，runbook 主线不依赖

**narrative `<job_id>` / `<n>` 等**：仅在 URL 模板（`workspace/<job_id>`）/ incident 编号示意场景，不是命令

## §15.13 v0.11 → v0.12 修订记录（Codex 十一审 2 P1 + 1 P2）

### P1（执行级，必修）

| # | v0.11 错误 | v0.12 修复 |
|---|---|---|
| P1-1 | `f_step67_collect_cleanup_inputs.sh` 中 `ADMIN_USER_ID=$(...)` 只是 shell 变量，Python 用 `os.environ.get("ADMIN_USER_ID")` 读取，未 `export` 会得到 `null`，后续 worker delete request 的 `user_id` 为空 | `echo ADMIN_USER_ID` 后立即 `export ADMIN_USER_ID`；Python 入口加 `if not admin_user_id: raise SystemExit(...)`，禁止生成 unsafe `cleanup_inputs.json` |
| P1-2 | cleanup inputs 只打印 `voices: N rows (expected 2...)`，不会在 0/1/多行时失败；SQL 只按 admin/provider/time window 过滤，可能误收非本次烟测 voice | SQL 额外限定 `tts_provider='cosyvoice'`、`platform='dashscope_mainland'`、`created_from='cosyvoice_clone_endpoint'`、`requires_worker IS TRUE`、`worker_provider='cosyvoice'`、`target_model='cosyvoice-v3.5-flash'`、`clone_sample_seconds IS NOT NULL`；Python 强制 `len(voices) == 2`，且必须一条 file path（`clone_sample_segment_ids IS NULL`）+ 一条 segments path（非空） |

### P2（文档一致性）

| # | v0.11 问题 | v0.12 修复 |
|---|---|---|
| P2-1 | §15.11 仍写 `<DashScope voice_id>` / `<admin uuid>` / `<oss_url_from_audit>` 是“执行前需替换”的残留占位，与 v0.11 的 JSON 自动收集策略矛盾 | 改成 v0.12 占位符状态说明：cleanup 主路径无手工 voice/admin/OSS 占位；唯一人工入口是 §6.5 的 smoke job_id；SQL 模板占位均 marked `DO NOT RUN` |

## §15.14 v0.12 → v0.13 修订记录（Codex 自审 2 P1）

| # | v0.12 问题 | v0.13 修复 |
|---|---|---|
| P1-1 | §3.2 仍保留裸 `docker compose ... alembic ...` 命令，违反 §2.0.0/§4.2 的“远端命令必须经 SSH-US-Via-154 + 根 compose + --env-file”约束；执行者从本地 PowerShell 复制会直接失败或在错环境运行 | 新增 `f_step32_migration.sh`，把 dry-run SQL、destructive SQL guard、`alembic upgrade head`、`alembic current` 全放进远端临时脚本；用 SCP + SSH wrapper 执行 |
| P1-2 | §4.3 仍用不带 commit marker 的 `git archive`，但后续又要求 staging commit id 与 HEAD 一致；§16 还把 `.git_archive_commit` 作为开放问题 | 打包命令固定生成 `D:\Claude\temp\.git_archive_commit`，通过 `git archive --add-file=...` 放进 archive root；解包 staging 前先 `rm -rf`，避免旧文件残留；`f_step43_sync.sh` 缺 marker 时 hard fail；§16 不再把 commit marker 列为开放问题 |
| P1-3 | §3.3 schema rollback 仍是裸 `docker compose ... alembic downgrade ...` 命令；rollback 场景更容易照抄错环境 | 新增 `f_rollback_schema_pretraffic.sh`，默认回滚到 029，支持 `ROLLBACK_TARGET=030` 只退 031；同样走 SCP + SSH wrapper |

## §15.15 v0.13 → v0.14 修订记录（Codex 自审二轮 2 P1）

| # | v0.13 问题 | v0.14 修复 |
|---|---|---|
| P1-1 | `docker compose exec gateway alembic ...` 在 SSH 脚本中默认尝试分配 TTY，非交互环境可能卡住或报 `the input device is not a TTY` | 所有 alembic `exec` 改为 `exec -T gateway ...`，包括 F.0 head check、F.1 upgrade、pre-traffic schema rollback |
| P1-2 | app 树 tar 备份 exclude 太窄，可能把 `node_modules` / `.next` / pytest cache / pycache 等大目录打进备份，导致部署窗口耗时和磁盘占用不可控 | §2.5 tar 增加 `*/node_modules`、`*/.next`、`*/.pytest_cache`、`*/__pycache__`、`./staging` 等排除，并在通过标准中明确 app.tar.gz 不应包含这些可重建目录 |

## §15.16 v0.14 → v0.15 修订记录（Codex 自审三轮）

| # | v0.14 待审点 | v0.15 决议 |
|---|---|---|
| P1-1 | §16 Layer 2 hard kill 是否必须先做 admin UI | F 阶段接受 CLI hard kill；UI toggle 拆到 F 后专项，不阻塞 deploy |
| P1-2 | §6.2 只做 uploader factory/config probe，不实际 PUT/GET | §6.2 强制真实 OSS round-trip：5 秒 WAV PUT、signed GET sha256、delete、删除后 403/404 |
| P1-3 | §6.8 cleanup 依赖 app 容器 import `src.services...client_factory`，但 F.0 未验证 | 新增 §2.4b app 容器 import sanity，失败则不进 F.1 |

## §15.17 v0.15 → v0.16 修订记录（F.0 实测修正）

| # | v0.15 问题 | v0.16 修复 |
|---|---|---|
| P1-1 | F.0 in-flight SQL 使用 `jobs.current_step`，生产 DB 报 `column "current_step" does not exist`，提示真实列为 `jobs.current_stage` | §2.1 全部 in-flight SQL 改用 `current_stage`，包括 blocker 列表、running worker query 和示例查询 |

## §15.18 v0.16 → v0.18 修订记录（F.0 实测修正）

| # | 问题 | 修复 |
|---|---|---|
| P1-1 | 生产当前 alembic 已是 `030_cosyvoice_clone_metadata`，不是早期 runbook 假设的 029；031 migration 文件也不在当前旧 gateway image/build context 中 | §2.2 改为期望 030；F.1 实施时先完成代码同步/新 gateway image 构建，再用 one-off gateway container 跑 031 migration，最后 recreate 服务 |
| P1-2 | `docker exec aivideotrans-app python3 <<'PYEOF'` / `docker exec aivideotrans-gateway python3 <<'PYEOF'` 没有 `-i`，heredoc 不会送进容器，脚本可能空跑 | §2.4b 和 §6.2 全部改为 `docker exec -i ... python3 <<'PYEOF'` |

## §15.19 v0.18 → v0.19 修订记录（F.0 实测修正）

| # | 问题 | 修复 |
|---|---|---|
| P1-1 | app 树备份 tar 使用 `**/__pycache__` 风格排除，生产 tar 实测仍打入 `gateway/__pycache__` / `gateway/pan/__pycache__` | §2.5 改为 tar 兼容的 `*/__pycache__` + `*/__pycache__/*`，并对 `node_modules` / `.next` / `.pytest_cache` 使用同款目录+内容双排除 |

## §16 v0.15 部署前结论（Codex 自审三轮）

v0.14 的 3 个待审点已在 v0.15 明确：

- **Layer 2 hard kill**：F 阶段接受 CLI `f_layer2_disable_clone.sh` 作为生产应急路径；新增 admin UI `worker_enabled` toggle 不阻塞 F，拆到 F 后独立任务。
- **OSS probe**：§6.2 已升级为真实 5 秒 WAV 的 OSS PUT → signed GET → sha256 校验 → DELETE → 删除后 403/404，不再只做 factory/config probe。
- **app 容器 import sanity**：§2.4b 已加入 F.0 必跑检查，证明 `from src.services.mainland_worker.client_factory import build_client_from_env` 可 import 且返回 client。

**v0.15 结论：** runbook 层面没有继续保留的部署前开放问题。进入生产前仍必须先把本 spec 单独 commit 到 main，再按 §2 F.0 顺序执行；F.0 任一检查红则停止，不进入 F.1。
