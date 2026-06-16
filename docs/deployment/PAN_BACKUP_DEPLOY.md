# Admin Pan Backup — 部署 Runbook (Phase 10)

实施计划:[docs/plans/2026-05-14-admin-pan-backup-implementation-plan.md](../plans/2026-05-14-admin-pan-backup-implementation-plan.md)
设计文档:[docs/plans/2026-05-13-admin-pan-backup-design.md](../plans/2026-05-13-admin-pan-backup-design.md)

这是把 admin pan backup 功能从代码完成 (Phase 1-9 全绿) 推到生产 + 灰度 1 周的操作手册。**实际命令在远程 US 主机执行**,本地 (Windows) 只跑 `D:\daili\scripts\*-Via-154.cmd` 系列脚本做文件传输 + 容器重启。

## 总体节奏

- **D-3 前** — Baidu 开放平台审核 (1-3 个工作日,无法加速)
- **D 日** — env 配置 + alembic + 容器部署 (~2h,低峰时段 BJT 02:00-04:00)
- **D+1 ~ D+7** — 灰度: 每日跑 `r2_observability.py` 看事件,失败率 < 5% 才解锁下一步
- **D+8** — `AVT_PAN_AUTO_ARCHIVE_DRY_RUN=false` 启动自动归档
- **D+8 ~ D+30** — 41 个历史任务分批回填,每天 5-10 个,总耗时预期 10-40h

## 前置条件 (D-3 前必须办完)

- [ ] Baidu 开放平台账号已申请 AppKey/SecretKey + 应用审核通过
  - 登录 https://pan.baidu.com/union/console
  - 创建应用,scope 至少包含 `basic`,`netdisk`
  - **重定向 URI 填**: `https://aitrans.video/api/admin/pan/callback`
  - 审核期 1-3 工作日 (个人开发者) / 当天 (企业)
- [ ] Fernet 加密 key 已生成 + 双备份
  - 生成: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
  - 备份 1: 1Password 共享保险库
  - 备份 2: 物理纸条 (保险柜或银行保险箱)
  - **丢失后果**: 所有 admin pan 凭据全部失效,必须重新 OAuth (用户体验差但不丢业务数据)
- [ ] 生产 PG 当前状态空闲: in-flight 任务 (queued/running) 数量 < 3
  - 检查: `docker exec aivideotrans-postgres psql -U avt -d aivideotrans -c "SELECT count(*) FROM jobs WHERE status IN ('queued','running')"`

## D 日 — 部署步骤

### 1. 配置环境变量 (生产 `.env`)

US 主机上 `/opt/aivideotrans/.env` 增加:

```bash
# Pan Backup — 主开关
AVT_ENABLE_PAN_BACKUP=true

# Baidu OAuth (从开放平台 console 拿)
AVT_BAIDU_PAN_APPKEY=<your_appkey>
AVT_BAIDU_PAN_APPSECRET=<your_appsecret>
AVT_BAIDU_PAN_REDIRECT_URI=https://aitrans.video/api/admin/pan/callback

# Token 加密 (前置条件 §2 生成的 Fernet key,base64 字符串)
AVT_PAN_TOKEN_ENCRYPTION_KEY=<fernet_key_base64>

# 自动归档 (灰度第 1 周保持 dry_run=true)
AVT_PAN_AUTO_ARCHIVE_ENABLED=false       # 第 1 周禁用 cron,只手动 backup
AVT_PAN_AUTO_ARCHIVE_DRY_RUN=true        # 第 1 周即使开了 cron 也只 log
AVT_PAN_AUTO_ARCHIVE_DAYS=30             # 任务 updated_at > 30d 进候选
AVT_PAN_AUTO_ARCHIVE_HOUR_BJT=3          # 凌晨 3 点 BJT 跑候选扫描
AVT_PAN_AUTO_ARCHIVE_MAX_PER_RUN=5       # 单次 cron 最多 enqueue 5 个

# 周末 orphan cleanup (默认 Sat 04:00 BJT,不用改)
AVT_PAN_ORPHAN_CLEANUP_WEEKDAY=5

# Stale reaper (大任务可调到 6-8h)
AVT_PAN_TASK_STALE_HOURS=4
```

**记忆点**: env 文件改完必须 `docker compose --env-file ... up -d` 才能让 gateway 读到新值;**单纯 `docker restart` 不会重新插值 env_file** (踩过坑见 `feedback_compose_env_file_recreate.md`)。

- [ ] `.env` 编辑完毕
- [ ] `grep -c "AVT_PAN" /opt/aivideotrans/.env` 输出 `>= 8`

### 2. 部署 app 容器 (T10.3)

**为什么先 app**: Phase 9 改了 `src/services/jobs/events.py::SUPPORTED_EVENT_TYPES` (加 8 个 pan.*),app 容器读这个常量来 validate JobEvent,不重启的话历史代码不认新 event_type 会拒收。

- [ ] 选 BJT 02:00-04:00 的低峰窗口
- [ ] 再次 confirm in-flight 任务 < 3 (`psql ... WHERE status IN ('queued','running')`)
- [ ] 从本地 Windows 推 src/ 变更:

  ```cmd
  D:\daili\scripts\Deploy-Via-154.cmd
  ```

  app 的 `src/` 是 bind mount,这一步只是 `scp -r src/ us:/opt/aivideotrans/app/src/`,无需 image rebuild。

- [ ] 等所有 in-flight pipeline 自然完成或人工 `docker exec aivideotrans-app python -c "from services.jobs.process_runner import ProcessJobRunner; ProcessJobRunner.drain(timeout_s=300)"` (drain helper 不存在时人工等)
- [ ] **轻重启 (不 recreate)**:

  ```bash
  docker restart aivideotrans-app
  ```

- [ ] 验证新 vocab 已加载:

  ```bash
  docker exec aivideotrans-app python -c "from services.jobs.events import SUPPORTED_EVENT_TYPES; pan = [t for t in SUPPORTED_EVENT_TYPES if t.startswith('pan.')]; print(len(pan), pan)"
  ```

  应该输出 `8 ['pan.backup.failed', 'pan.backup.started', 'pan.backup.succeeded', 'pan.residue_cleanup.completed', 'pan.restore.failed', 'pan.restore.started', 'pan.restore.succeeded', 'pan.token_revoked']`

### 3. 部署 gateway 容器 (T10.4)

gateway 改动是 image 级 (Dockerfile + 整 codebase),必须 rebuild image,然后 alembic migrate,然后 recreate gateway 容器。

- [ ] 推 gateway image + 代码:

  ```cmd
  D:\daili\scripts\Deploy-Via-154.cmd
  ```

  (这个脚本会:scp 整 repo,远端 `docker compose build gateway`)

- [ ] SSH 到 US 主机:

  ```cmd
  D:\daili\scripts\SSH-US-Via-154.cmd
  ```

- [ ] 跑 alembic upgrade (添加 3 张 pan_* 表 + Job.status enum 扩展):

  ```bash
  docker exec aivideotrans-app python -m alembic \
    -c /opt/aivideotrans/app/gateway/alembic.ini upgrade head
  ```

- [ ] 验证 PG 状态:

  ```bash
  docker exec aivideotrans-postgres psql -U avt -d aivideotrans -c "\dt pan_*"
  ```

  应该列出 `pan_credentials`, `pan_oauth_states`, `pan_backup_records` 3 张表 (或对应名称)。

- [ ] **只重建 gateway** (避免再次重启 app):

  ```bash
  docker compose -f docker-compose.yml --env-file /opt/aivideotrans/.env \
    up -d --no-deps --force-recreate gateway
  ```

  `--no-deps` 是关键 —— 避免连带 recreate app (Compose 默认会因 env_file 变更触发依赖容器 recreate,见 `feedback_compose_env_file_recreate.md`)。

- [ ] 健康检查:

  ```bash
  curl -k https://aitrans.video/healthz
  curl -k https://aitrans.video/api/admin/pan/status -b "avt_session=<admin_cookie>"
  ```

  status 端点没登录会返 401,这是预期。下一步要先在浏览器登录获取 cookie。

### 4. 首次 OAuth + smoke (T10.5)

- [ ] 浏览器 admin 登录 `https://aitrans.video`,打开 dev tools 抓 `avt_session` cookie 备用
- [ ] 访问 `/admin/pan/dashboard`,看到 "未连接百度网盘" + "连接" 按钮
- [ ] 点击 "连接",跳转到 Baidu 授权页 → 同意 → 自动跳回 dashboard
- [ ] dashboard 显示 "已连接 ✓ + 配额 X GB / 2 TB"
- [ ] **选一个小测试任务 (< 1GB)** 跑 smoke (见 `scripts/pan_backup_smoke.py`):

  ```bash
  # 在本地 Windows 跑 (从开发机访问生产 gateway)
  python scripts/pan_backup_smoke.py \
    --gateway https://aitrans.video \
    --cookie 'avt_session=<admin_cookie>' \
    --job-id <small_test_job_id>
  ```

  smoke 会:
  1. GET /status 确认 connected
  2. POST /backups 入队 backup 任务
  3. 轮询任务状态直到 completed (最多 4h)
  4. 验证 `backup_records` 行存在 + status=`uploaded`
  5. POST /restores 入队 restore
  6. 轮询直到 completed (最多 1h)
  7. 验证 `backup_records` 行变为 status=`restored`

  典型 1GB 任务全流程 ~15-30 分钟 (取决于跨境网速)。

- [ ] smoke 返回 exit 0 + 输出 `[ok] smoke PASSED end-to-end`
- [ ] 验证 events JSONL:

  ```bash
  docker exec aivideotrans-gateway python \
    /opt/aivideotrans/app/scripts/r2_observability.py --since 1h --format json | \
    python -c "import sys, json; d=json.load(sys.stdin); print(json.dumps(d['pan'], indent=2))"
  ```

  应该看到 `started:1, succeeded:1, failed:0` (或 `started:2, succeeded:2` 如果 restore 也算 started)

- [ ] 验证本地 project_dir 还在 + 内容完整 (restore 后):

  ```bash
  docker exec aivideotrans-app ls -la /opt/aivideotrans/data/projects/<job_id>/
  ```

## D+1 ~ D+7 — 灰度第 1 周 (T10.6)

每天早上跑一次 observability,**任何指标异常立刻停手动 backup,不要进 T10.7**:

```bash
# 每日检查 (可加 cron):
docker exec aivideotrans-gateway python \
  /opt/aivideotrans/app/scripts/r2_observability.py --since 24h
```

健康基线 (P99 实测后调):
- `pan.backup.failed` 占比 < 5%
- `pan.restore.failed` 占比 < 5%
- `pan.token_revoked` 当日为 0 (出现表示 Baidu 拒绝 refresh,需立刻人工排查)
- `pan.residue_cleanup.completed` 当日为 0 (出现表示有 backup 失败到了 stale_reaper Pass 2,需查具体任务)

第 1 周每天手动 backup 1-3 个真任务 (一定要先 restore 验证完整性后再继续下一个),逐步建立信心。

- [ ] D+1 检查通过
- [ ] D+2 检查通过
- [ ] D+3 检查通过
- [ ] D+4 检查通过
- [ ] D+5 检查通过
- [ ] D+6 检查通过
- [ ] D+7 检查通过

## D+8 — 切到 auto-archive (T10.7)

灰度 1 周稳定后,可以打开 cron 自动归档,但**先 dry-run 一晚**:

- [ ] `.env` 改: `AVT_PAN_AUTO_ARCHIVE_ENABLED=true` (保持 `AVT_PAN_AUTO_ARCHIVE_DRY_RUN=true`)
- [ ] `docker compose --env-file ... up -d --no-deps --force-recreate gateway`
- [ ] 等到第二天凌晨 03:30 BJT 后,grep cron 输出:

  ```bash
  docker logs aivideotrans-gateway 2>&1 | grep pan_archive_scanner | tail -20
  ```

  应该看到 `dry_run=True candidates=N enqueued=0` (没真上传,只 log)。

- [ ] candidates 列表合理 (确认 30d+ 的旧任务在列里,active 任务不在)
- [ ] **关闭 dry-run**: `.env` 改 `AVT_PAN_AUTO_ARCHIVE_DRY_RUN=false`
- [ ] `docker compose ... up -d --no-deps --force-recreate gateway`
- [ ] 等下一次 03:30 BJT 后,确认 `enqueued=5` (或 max_per_run 设的值) + 真有 5 个任务进入 archiving 状态

## D+8 ~ D+30 — 41 任务回填 (T10.8)

按 spec §16.4,分批每天 5-10 个,总耗时预期 10-40h:

- 让 auto-archive cron 自然处理 30d+ 的任务 (每天 5 个 = ~10 天清完 50 个)
- 或者通过 dashboard 的 "批量备份" 入口手动加速 (POST /api/admin/pan/backups/batch)

监控指标:
- 本地 disk 释放进度 (`df -h /opt/aivideotrans/data/projects`)
- Baidu pan 占用 (dashboard `/admin/pan/status` 显示)
- R2 artifacts 减少 (每个归档任务的 R2 对象会被删除)

- [ ] D+15 进度检查: 已归档 ≥ 20 个任务 / 本地 disk 释放 ≥ 40GB
- [ ] D+30 完成: 41 个候选任务全部归档 + 本地 disk 释放 ≥ 100GB

## 验收清单 (Acceptance Criteria)

部署完整通过的判定 (来自 plan §Acceptance Criteria):

- [ ] alembic 029 在生产 PG 跑通,`\dt pan_*` 显示 3 表
- [ ] 所有新 test 文件 + 现有 test 全绿 (CI 已绿)
- [ ] OAuth flow 跑通,admin 看到"已连接百度网盘 ✓ + 配额"
- [ ] 手动 backup 1 个真任务,backup_records 行 + pan 真有 tar.gz + 本地+R2 真空 + 状态 archived
- [ ] 手动 restore 同任务,数据完整 (sha256 + inventory 全过)
- [ ] 假场景:停 Baidu 网络让 refresh 失败 24h → 看 UI 红 banner + notifications 出现
- [ ] 假场景:`docker kill aivideotrans-gateway` 在 backup 中途 → 4h 后 stale_reaper 自动 forward-resolve 或 rollback
- [ ] 假场景:DELETE 唯一可恢复副本 → 412 + 提示
- [ ] 假场景:scanner dry-run 跑 1 晚,log 见 candidates,无 enqueue 发生
- [ ] 30d auto-archive 在 `dry_run=false` 后第一次触发,5 个任务被 enqueue
- [ ] `r2_observability.py` 显示 PAN 事件分组,1 周后 failure 率 < 5%
- [ ] 41 任务首次回填完成,本地 disk 释放 ≥ 100GB

## 回滚步骤

任何阶段出现 P0 问题立刻回滚:

```bash
# 1. 关掉主开关,所有 /admin/pan/* 端点立即返 404
sed -i 's/AVT_ENABLE_PAN_BACKUP=true/AVT_ENABLE_PAN_BACKUP=false/' /opt/aivideotrans/.env

# 2. 重建 gateway (alembic 的 schema 变更保留,只是端点不可达)
docker compose -f docker-compose.yml --env-file /opt/aivideotrans/.env \
  up -d --no-deps --force-recreate gateway

# 3. 确认主路径仍工作
curl -k https://aitrans.video/healthz
curl -k https://aitrans.video/api/admin/pan/status -b "avt_session=..." # 应该返 404
```

回滚不撤销 alembic migration (3 张 pan_* 表保留,但被 ORM 忽略)。要彻底删 schema 必须人工 `alembic downgrade` 到 028,会丢失 已经 backup 过的任务的 BackupRecord 行 (但 Baidu pan 上的 tar 还在,可手动 inventory 重建)。

## 风险登记

| # | 风险 | 缓解 |
|---|------|------|
| R1 | Baidu 个人开发者限流 (~500 req/min) | 重试 + backoff;超阈值主动 sleep,设 `AVT_PAN_TASK_STALE_HOURS=8` |
| R2 | 跨境 pan 上传中途中断,部分 chunk 留在 pan | 周末 orphan_cleanup 自动清;`uniq_backup_in_flight` 索引保护重试 |
| R3 | Fernet key 丢失 | 双备份 (1Password + 物理纸条),实施前必须确认两份都到位 |
| R4 | 首次 41 任务回填超 24h | 分批 + heartbeat 续命;阶段性中断不影响完成的 backup |
| R5 | 实测 4h stale_hours 对 60GB+ 任务不够 | env 调大到 6-8h;不改代码 |
| R6 | env_file 变更触发 app 容器误重启 | 永远用 `--no-deps --force-recreate gateway`,不要 `up -d gateway` |

## 故障排查 (Quick Reference)

| 现象 | 第一步排查 |
|------|----------|
| `/admin/pan/dashboard` 显示 "未连接" 但实际有 PanCredentials 行 | `docker exec aivideotrans-postgres psql -U avt -d aivideotrans -c "SELECT id, status, last_refreshed_at FROM pan_credentials"` 看 status 是否被标 `revoked` |
| OAuth callback 返 400 "Invalid state" | `pan_oauth_states` 表的 state token TTL=5min,可能用户卡在中间页太久;让用户重新点 "连接" |
| backup 卡在 `archiving` 不动 | 看 `backup_records.heartbeat_at`;若 > 4h 没更新,等下一次 stale_reaper (每 30min) 自动 reap |
| 任务卡 `archiving` 且当前代 backup_records 全是 `failed`(或无记录) | 2026-06-10 事故形态(运维手工中止备份只标了 BR failed,没翻 Job.status)。stale_reaper **Pass 3** 会在 `Job.updated_at` 超过 stale 阈值后自动翻回 `succeeded`;要立即处理/主动中止备份,用下方"受控状态恢复",**禁止裸 UPDATE** |
| backup 显示 succeeded 但 `pan.backup.succeeded` 事件没出现 | 检查 `settings.jobs_dir` 是否可写 + gateway 容器是否 mount 了 `/opt/aivideotrans/data/jobs` (rw) |
| 通知 body 显示 `{display_name}` / `{reason}` 原文 | `_PAYLOAD_ALLOWLIST` 漏了对应 key;最新版本 (commit 842b66d 之后) 已修 |
| `r2_observability.py --since 24h` 显示 pan 总数 0 但确实 backup 过 | gateway 容器没 mount `jobs/` bind volume,或者 stage 字段写错;先 `cat /opt/aivideotrans/data/jobs/<job_id>.events.jsonl` 看有没有行 |

## 受控状态恢复 (postmortem P2b)

**运维需要中止一个进行中的 pan 备份、或修复中止后的错位状态时,唯一许可入口是
`pan.status_mutator.rollback_archive_attempt`。禁止对 `jobs` / `backup_records`
做裸 `UPDATE` 或只改一半状态** —— 2026-06-02 的手工中止只把 backup_records 标了
`failed`、没把 Job.status 翻回 `succeeded`,结果三个 stale_reaper pass 都不命中、
`create_backup` 又要求 `succeeded`,任务在 `archiving` 死锁 7 天
(job_c31bd38126fd47ed8c2d3c1749c15ccf,2026-06-10 才发现)。

`rollback_archive_attempt(user_id, job_id, conn=conn, reason=...)` 做的事:

1. `pg_try_advisory_lock` 探测 —— executor 还活着就拒绝(不绕锁);
2. Job 状态不在 `('archiving', 'succeeded')` 时拒绝(restore 归 Pass 1 管);
3. 当前 `edit_generation` 下存在 `uploaded` BR 时拒绝(已过 COMMIT POINT,
   归 Pass 2 / residue_cleanup 收尾成 `archived`,不能回滚成 `succeeded`);
4. 单事务内:`uploading` BR → `failed`(带 reason + completed_at),
   Job `archiving` → `succeeded`(PG + JSON mirror 同步)。幂等,重复调用安全。

```bash
docker exec -i aivideotrans-gateway python - <<'PY'
import asyncio, uuid
from database import engine
from pan.status_mutator import rollback_archive_attempt

async def main():
    async with engine.connect() as conn:
        summary = await rollback_archive_attempt(
            uuid.UUID('<user_id>'), '<job_id>', conn=conn,
            reason='ops abort: <为什么中止,写进 backup_records.error_message>',
        )
    print(summary)

asyncio.run(main())
PY
```

不归它管的形态:`uploaded` 卡 `archiving`(等 Pass 2 + residue_cleanup)、
`restoring` 卡死(等 Pass 1 按 project_dir 存在性 forward/rollback)。
这两类**等 stale_reaper**,不要手工介入。
