# Admin 一键备份到百度网盘 — Design Spec

**日期**: 2026-05-13
**状态**: Design 完成,等 spec review + user 确认后进 implementation plan
**关联文档**:
- [`2026-05-07-disk-relief-via-r2-publisher-and-ttl.md`](2026-05-07-disk-relief-via-r2-publisher-and-ttl.md) §11.8(本 design 的上游 placeholder)
- CLAUDE.md「付费 API 不能自动调用」、「容器代码部署注意」
- `feedback_terminal_state_single_entry.md`(mirror_job_terminal_state 单一入口)
- `feedback_r2_publisher_consumer_contract.md`(R2 文件名 = local stem)

---

## 1. 背景与目标

### 1.1 背景

Disk-relief 方案 v4 落地后,**admin 账户**(`gateway/project_cleanup.py:121` `if role_snapshot == "admin": return False`)在 cleanup 路径上永久豁免,导致单 admin 用户的 41 个 succeeded 任务累积占用 **141 GB**(2026-05-12 磁盘 100% 事件的全部根因)。Stage B parity gate 启用也救不了——`_is_expired` 在 admin 豁免分支 short-circuit。当前救急方案是手工扩容 +50 GB,长期需要"admin 主动归档"能力。

### 1.2 目标

让 admin 能把**自己**的 succeeded 任务**完整备份到自己的百度网盘**,本地 `project_dir` + R2 artifacts 一起释放,后续需要再剪辑时一键拉回继续编辑。

### 1.3 非目标(显式排除)

- ❌ 不为非 admin 用户提供此功能(MVP 范围)。Schema 留 user_id 列方便未来扩,但所有端点 + scanner 都加 `role='admin'` 守门。
- ❌ 不做多 provider 抽象抽到完美。MVP 只接百度网盘,但写一个 `PanProvider` 协议留接口,未来加 OneDrive / 阿里云盘按这个协议补就行。
- ❌ 不做"自动 backup 到本项目自己的 R2 second bucket"(那是数据库灾备,不是 admin 归档)。
- ❌ 不动现有 `project_cleanup.py` 的 admin 豁免分支。Archive 是另一条主动路径,跟 cleanup 不混。
- ❌ 不做"懒 archive"(R2 留着 / 仅删 project_dir)。Archive 即"全删,只剩 pan 一份" + JobRecord PG metadata。

---

## 2. 关键决策汇总

| # | 决策 | 选择 | 备注 |
|---|------|------|------|
| D1 | 备份目的 | 为再剪辑/重新生成 | 需完整 project_dir(原视频+transcript+segments+tts WAV+配音视频+剪映工程),保真度优先 |
| D2 | Provider | 百度网盘 | admin 现有,1TB+ 容量。MVP 仅此一个;PanProvider 协议留扩展接口 |
| D3 | Post-backup 处置 | **自动**删本地 + R2 | 唯一能真正释放 141G 的路径 |
| D4 | Restore 范围 | MVP 含完整 Restore 按钮 | backup/restore 双向闭环 |
| D5 | 触发模式 | 手动 + 30d 自动 cron | Manual: admin 单点/批量;Auto: 03:30 BJT cron 扫 `admin + succeeded + updated_at < now-30d` |
| D6 | Archive 语义 | 严格 Archive | JobRecord 留 PG 作 metadata,R2+local 全删。"修改"按钮在 archived 任务上变 "Restore 后修改" |
| D7 | OAuth 接入 | 完整 Web Flow | `/admin/pan/connect` → Baidu 授权 → callback → 自动 refresh |
| D8 | 模块归属 | 全在 `gateway/` | 不动 `src/services/` |
| D9 | Restore 后 R2 重推 | ❌ 不同步,sweeper 异步 | 代价是 restore 后 5 min 内 download 走 local fallback,可接受 |
| D10 | Orphan cleanup 频率 | 单独脚本,周六 04:00 BJT | 跟现有 nightly cron 解耦 |
| D11 | 并发上限 | 全局 1 backup + 1 restore | semaphore in gateway process |
| D12 | Encryption | Fernet 对称,key 在 env | key 备份: 1Password 主拷贝 + 纸条冷拷贝(物理保险箱) |
| D13 | 通知通道 | `gateway/notifications_service.py:dispatch_event` | 不接邮件管道,走站内 |
| D14 | Feature flag | `AVT_ENABLE_PAN_BACKUP=false` 默认 | flag OFF 时所有 `/admin/pan/*` 端点返 404 |

---

## 3. 架构 + 模块拆分

**全部新代码在 gateway/ 容器**。理由:backup/restore 是 admin-only 低频功能,迭代期改动多,gateway 重启用户最多 0.5s 502;`docker restart aivideotrans-app` 会杀正在跑的 pipeline——代价不对称。

### 3.1 新增文件

| 文件 | 职责 |
|------|------|
| `gateway/pan/__init__.py` | 包入口 |
| `gateway/pan/baidu_pan_client.py` | OAuth code 兑换 / token refresh / 4MB 分片上传 / 下载 / list / delete。一个 class 实现 `PanProvider` 协议。 |
| `gateway/pan/provider_protocol.py` | `PanProvider` Protocol:`upload(local_path, remote_path)` / `download(remote_path, local_path)` / `list(prefix)` / `delete(remote_path)` / `get_quota()` |
| `gateway/pan/token_crypto.py` | Fernet encrypt/decrypt helper,key 从 `AVT_PAN_TOKEN_ENCRYPTION_KEY` 读 |
| `gateway/pan/manifest.py` | manifest_json 构造 + 解析 + file inventory(逐文件 sha256) |
| `gateway/pan/status_mutator.py` | `set_archive_status(job_id, new_status)`:写 Gateway PG `Job.status` + JSON store。**不**走 `mirror_job_terminal_state`(那是 JSON→PG 镜像方向 + terminal 专用,archive 状态非 credit-bearing,跳过 quota.settle) |
| `gateway/pan/archive_scanner.py` | 凌晨 cron 扫 30d candidate + enqueue |
| `gateway/pan/orphan_cleanup.py` | 周六 cron 扫 pan 远端文件 + 跟 PG 对账 + 删孤儿 + GC 过期 oauth_states |
| `gateway/pan/stale_reaper.py` | **新建** archiving/restoring 卡死兜底。Gateway 当前**没有** reap-stale 调度(verified:`gateway/job_terminal_mirror.py` 无 `reap_stale*` 函数),所以这是新 infra 不是扩展。挂在 gateway 启动期 background scheduler,每 30 min 扫一次 |
| `gateway/pan/auth.py` | OAuth Web Flow: `/admin/pan/connect`、`/admin/pan/callback`(原拟名 `gateway/auth_baidu_pan.py`,改放 pan/ 子包) |
| `gateway/admin_pan_api.py` | 10 个 admin 端点(见 §6) |

### 3.2 扩展现有文件

| 文件 | 改动 |
|------|------|
| `gateway/models.py` | 新增 `PanCredentials`、`BackupRecord`、`PanOauthState` model;`Job.status` 加 `archiving` / `archived` / `restoring` |
| `gateway/alembic/versions/028_pan_backup.py` | migration:加 3 表 + status enum 扩 + 索引 |
| `gateway/background_task_executors.py` | 加 `execute_pan_backup` / `execute_pan_restore` / `execute_pan_token_refresh` |
| `gateway/startup_checks.py` | 加 `validate_pan_backup_config`:flag 开启时校验 4 个 env 都设 |
| `gateway/config.py` | `GatewaySettings` 加 6 个字段(见 §5.1) |
| `gateway/logs_redactor.py` | mask 关键字加 `access_token` / `refresh_token` / `appsecret` |
| `gateway/notification_dispatch_map.py` | 注册 3 个新 recipe:`pan_token_revoked` / `pan_backup_failed` / `pan_restore_failed`(message template / level=warn 或 error / action_link 跳 `/admin/pan/dashboard`)。**漏注册会让 `dispatch_event` 静默返 None** |
| `src/services/jobs/events.py` | `SUPPORTED_EVENT_TYPES` 加 6 个 `pan.*` |
| `gateway/storage/event_log.py` | `_DOWNLOAD_EVENT_TYPES` **不改名**(line 58-59 注释明确"kept for git-blame continuity")— 只是 frozenset 集合加 6 个 pan.* |
| `scripts/r2_observability.py` | 加 PAN 事件 tracking + 渲染段(范围扩到 `download.* / stream.* / pan.*` 三组) |
| `tests/test_phase2_download_backend.py` | 更新契约测试 set 加 pan.* |
| `tests/test_r2_observability.py` | `test_script_event_vocab_in_sync_with_jobs_events` 扩前缀过滤 `download.* / stream.* / pan.*`(否则只过滤前两个不会发现 pan.* 漂移)|

### 3.3 perimeter 守卫

Gateway 不 top-level import `services.jobs.*`(因为 services.jobs 会传染拉入 pydub)。新代码继续走 `gateway/storage/job_store_reader.py` 的 JSON-only 协议读 JobRecord。

---

## 4. Schema 与状态机

### 4.1 JobRecord.status 扩展

```
existing: queued | running | succeeded | failed | cancelled | editing
+ archiving      (transient: backup 上传中)
+ archived       (终态: tar.gz 在 pan,本地+R2 已清)
+ restoring      (transient: restore 下载/解包中)
```

集合归属:
- `ACTIVE_JOB_STATUSES`: 加 `archiving`、`restoring`(避免 cleanup 误杀)
- `WORKER_ACTIVE_STATUSES`: 不加(worker 不动它们)
- `archived` 两集合都不进

### 4.2 状态机(archive/restore 相关边)

```
succeeded ──manual/cron──→ archiving ──成功──→ archived
                              │
                              └──失败──→ succeeded (rollback)

archived ──admin restore──→ restoring ──成功──→ succeeded
                              │
                              └──失败──→ archived (rollback)
```

### 4.3 新表 DDL(alembic 028)

**类型约定**:`users.id` 是 `UUID`(verified `gateway/models.py:20+`)。所有 FK 跟现有 model 一致用 UUID。

```sql
-- 单 user 单 provider 一行 token
CREATE TABLE pan_credentials (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                  UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider                 VARCHAR(32) NOT NULL,
    access_token_encrypted   BYTEA NOT NULL,
    refresh_token_encrypted  BYTEA NOT NULL,
    access_token_expires_at  TIMESTAMPTZ NOT NULL,
    scope                    VARCHAR(255),
    status                   VARCHAR(32) NOT NULL DEFAULT 'active',  -- active | revoked
    connected_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_refreshed_at        TIMESTAMPTZ,
    UNIQUE(user_id, provider)
);

-- 一次 backup attempt 一行;失败也留行
CREATE TABLE backup_records (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    job_id          VARCHAR(64) NOT NULL,  -- 不 FK jobs.job_id,允许 jobs 行删后 backup 行存活
    provider        VARCHAR(32) NOT NULL,
    remote_path     TEXT NOT NULL,
    size_bytes      BIGINT NOT NULL,
    sha256          VARCHAR(64) NOT NULL,
    manifest_json   JSONB NOT NULL,
    status          VARCHAR(32) NOT NULL,  -- uploading | uploaded | failed | restoring | restored | deleted
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,
    error_message   TEXT
);

-- 一个 (user, job, provider) 任何时刻最多一个**进行中**的 backup/restore
-- 注意:**不**含 'uploaded' — 否则 archived 任务被 restore→edit 后无法再 archive
CREATE UNIQUE INDEX uniq_backup_in_flight
    ON backup_records (user_id, job_id, provider)
    WHERE status IN ('uploading', 'restoring');

CREATE INDEX idx_backup_user_status ON backup_records (user_id, status);
-- 给 archive_scanner 的 NOT EXISTS 子查询用(WHERE user_id=? AND job_id=? AND status IN (...))
CREATE INDEX idx_backup_user_job ON backup_records (user_id, job_id);

-- OAuth CSRF state(10 min TTL)
CREATE TABLE pan_oauth_states (
    token       VARCHAR(64) PRIMARY KEY,
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at  TIMESTAMPTZ NOT NULL
);
```

**`pan_oauth_states` 表的过期清理**:每次 admin 点"连接百度网盘"加 1 行,几年累积不算大但需要 GC。由 `orphan_cleanup.py` 周六 cron 顺手 `DELETE FROM pan_oauth_states WHERE expires_at < now();` 处理。

### 4.4 manifest_json 结构

```json
{
  "backup_format_version": 1,
  "created_at_utc": "2026-05-13T10:00:00+00:00",
  "source_host": "hetzner-us",
  "job_record": { "...完整 JobRecord JSON 快照..." },
  "r2_artifacts_snapshot": [
    {"artifact_key": "publish.dubbed_video", "edit_generation": 0, "state": "pushed", "r2_key": "..."}
  ],
  "file_inventory": [
    {"path": "transcript/s2_review_result.json", "size": 12345, "sha256": "abc..."},
    {"path": "publish/dubbed_video.mp4", "size": 654321, "sha256": "def..."}
  ]
}
```

**双份存储**:
- PG `backup_records.manifest_json` (查询 / 列表用)
- tar.gz 第一条 entry `manifest.json` (自描述,PG 丢了也能 restore)

---

## 5. 配置

### 5.1 新增 env vars

```bash
# Feature flag
AVT_ENABLE_PAN_BACKUP=false       # 默认关,生产开 true

# Baidu OAuth
AVT_BAIDU_PAN_APPKEY=             # 百度开放平台应用 appkey
AVT_BAIDU_PAN_APPSECRET=          # appsecret
AVT_BAIDU_PAN_REDIRECT_URI=https://aitrans.video/admin/pan/callback

# Token 加密
AVT_PAN_TOKEN_ENCRYPTION_KEY=     # Fernet key, 32B base64
                                  # python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# 调度
AVT_PAN_AUTO_ARCHIVE_DAYS=30      # 自动 archive 阈值(天)
AVT_PAN_AUTO_ARCHIVE_HOUR_BJT=3   # cron 触发小时
AVT_PAN_ORPHAN_CLEANUP_WEEKDAY=5  # 周六=5

# 上传配置
AVT_PAN_UPLOAD_CHUNK_BYTES=4194304   # 4MB
AVT_PAN_TASK_STALE_HOURS=4           # archiving/restoring 卡多久 reap。默认 4h(不是 2h),
                                     # 因为 §16.4 实测 100GB+ 跨境上传单任务可能合法用 2-3h。
                                     # 2h 太激进会误杀健康长任务。运营遇到 5h+ 单任务可调到 6-8h。
```

### 5.2 一次性 Baidu 开放平台配置

Admin 操作(MVP 之前完成):
1. [百度网盘开放平台](https://pan.baidu.com/union/apply) 申请个人开发者认证(1-3 工作日审核)
2. 创建应用,授权 scope `basic,netdisk`,填回调域名 `aitrans.video`
3. 拿到 appkey / appsecret 写到 `.env`

### 5.3 Startup 校验

`gateway/startup_checks.py:validate_pan_backup_config`:

```python
def validate_pan_backup_config(settings: GatewaySettings) -> None:
    if not settings.enable_pan_backup:
        return  # flag OFF, skip
    required = [
        ('AVT_BAIDU_PAN_APPKEY', settings.baidu_pan_appkey),
        ('AVT_BAIDU_PAN_APPSECRET', settings.baidu_pan_appsecret),
        ('AVT_BAIDU_PAN_REDIRECT_URI', settings.baidu_pan_redirect_uri),
        ('AVT_PAN_TOKEN_ENCRYPTION_KEY', settings.pan_token_encryption_key),
    ]
    missing = [n for n, v in required if not v]
    if missing:
        raise CriticalStartupError(
            f"AVT_ENABLE_PAN_BACKUP=true but missing required env: {missing}"
        )
    # Validate key is a real Fernet key (decodable base64, 32 bytes)
    try:
        Fernet(settings.pan_token_encryption_key.encode())
    except Exception as exc:
        raise CriticalStartupError(f"AVT_PAN_TOKEN_ENCRYPTION_KEY invalid: {exc}")
```

---

## 6. API endpoints

所有路径走 `require_admin` 依赖;feature flag OFF 时返 404。

| Method | 路径 | 用途 |
|--------|------|------|
| `GET`    | `/admin/pan/status`                       | 连接状态 + 网盘配额 + 上次 refresh |
| `GET`    | `/admin/pan/connect`                      | 302 跳 Baidu 授权页 |
| `GET`    | `/admin/pan/callback`                     | 接 OAuth code,写 token,302 dashboard |
| `DELETE` | `/admin/pan/credentials`                  | 断开连接(标记 status='revoked') |
| `POST`   | `/admin/pan/backups`         {job_id}     | 手动单任务 backup(enqueue) |
| `POST`   | `/admin/pan/backups/batch`   {job_ids[]}  | 批量 enqueue(每行单独 advisory lock,无大事务) |
| `GET`    | `/admin/pan/backups`                      | 列 backup_records,可过滤 status |
| `GET`    | `/admin/pan/backups/{id}/manifest`        | 返 manifest_json |
| `POST`   | `/admin/pan/restores`        {job_id}     | enqueue restore(取最新 uploaded backup_record) |
| `DELETE` | `/admin/pan/backups/{id}`                 | 同时删 PG 行 + 远端 tar.gz |

### 任务列表 UI 联动

- 现有 `/workspace` 列表页:succeeded 行加按钮"备份到网盘"(条件:admin + pan connected);多选模式加"备份选中"
- archived 行:灰 + badge,只显示"Restore"按钮
- 状态过滤器加 "archived" 选项,默认折叠

---

## 7. Backup flow(`execute_pan_backup`)

```
Trigger
  ├ Manual:  POST /admin/pan/backups   ──┐
  └ Cron:    archive_scanner.py 03:30   ──┴──→ enqueue background_task('pan_backup', {job_id, user_id})

Executor (gateway/background_task_executors.py:execute_pan_backup)
   ── Precondition ──
   0. 读 PG Job.status,要求 == 'succeeded'。其他(editing/failed/cancelled) 抛 412
      原因:scanner 已过滤,但手动端点也走这条 executor,深度防御

   ── Pre-commit(失败 → rollback succeeded,数据无损)──
   a. PG advisory lock hash(user_id, job_id) — 防同任务并发 backup
   b. set_archive_status(job_id, 'archiving')
      ├ 写 Gateway PG Job.status = 'archiving'
      ├ 写 jobs/{job_id}.json status='archiving'(用 services._file_lock 保护)
      └ ❌ 不走 mirror_job_terminal_state(那是 JSON→PG 镜像 + terminal-only quota.settle)
   c. INSERT backup_records (status='uploading')
   d. 构建 manifest_json:JobRecord 快照 + r2_artifacts 快照 + file inventory(逐文件 sha256+size)
   e. 流式 tar.gz 到 /tmp/pan_backup_{job_id}_{ts}.tar.gz(manifest.json 第一条 entry + project_dir 全内容)
   f. 算 tar.gz 总 sha256
   g. Baidu Pan 上传(decrypt access_token → precreate → 4MB 分片 PUT → finish)
   h. HEAD 远端验证 size 一致
   i. UPDATE backup_records SET status='uploaded', sha256, size_bytes, completed_at  ◄── COMMIT POINT
      此刻 tar.gz 在 pan + sha256 已知 + size 匹配,**用户数据已安全**,后续任一步失败 archived 不回滚

   ── Post-commit cleanup(失败 → 状态仍 archived,后台重试残留)──
   j. shutil.rmtree(project_dir)
      ├ 安全守卫:复用 gateway/project_cleanup.py:_is_safe_project_dir(path, safe_roots=DEFAULT_SAFE_PROJECT_ROOTS)
      └ False / 失败 → 记 error_message,continue(局部失败不卡 archive 主路径)
   k. 删 R2 artifacts(local **之后**做,顺序很重要)
      ├ for each entry in jobs.r2_artifacts:
      │   ├ r2_client.delete_object(Bucket, Key=entry.r2_key) — 失败 log,continue
      │   └ 仅 delete_object 成功才从 jobs.r2_artifacts JSONB 移除该 entry
      └ 顺序理由:local 先于 R2。R2 是 last-resort 数据冗余,优先保住 → 万一删 R2 失败,
         R2 残留不影响用户体验(JobRecord=archived,sweeper 看 status 不会重推),
         R2 残留由周末 orphan_cleanup 顺手清(扫 jobs.r2_artifacts 跟实际 R2 对账)。
         ❌ 不能 R2 先 local 后 — 万一 local 删失败要 rollback,R2 已没,sweeper 会试图重推已删的 local。
   l. set_archive_status(job_id, 'archived')
   m. 释放 advisory lock
   n. emit_event('pan.backup.archived')
```

**Pan 远端路径约定**: `/apps/AIVideoTrans/backups/{job_id}_{ts:YYYYMMDD-HHMMSS}.tar.gz`。`ts` 后缀允许同任务多次 backup(restore → edit → re-archive 流程会产生第二份 tar);UNIQUE 索引只防"in-flight 重叠",不阻塞历史多次。

**Commit point 语义**: 步骤 i 是事务边界。之前任一步失败 → `set_archive_status(job_id, 'succeeded')` + `UPDATE backup_records status='failed', error_message=...`,完全干净回滚。之后任一步失败 → status 仍是 archived(数据已安全),跳过失败步记录 error_message,周末 cleanup 兜底。

---

## 8. Restore flow(`execute_pan_restore`)

```
Trigger
  └ Manual: POST /admin/pan/restores   ──→ enqueue background_task('pan_restore', {job_id, user_id})

Executor (gateway/background_task_executors.py:execute_pan_restore)
   a. SELECT 最新 backup_records WHERE (user_id, job_id) AND status='uploaded'
   b. JobRecord.status = restoring
   c. UPDATE backup_records SET status='restoring'
   d. decrypt access_token,refresh if needed
   e. 流式下载 tar.gz 到 /tmp/pan_restore_{job_id}_{ts}.tar.gz
   f. 校验本地 sha256 == backup_records.sha256
      └ 不匹配 → 报警 + 回滚 archived
   g. tar 读第一条 entry manifest.json:
      ├ backup_format_version == 1
      └ job_record.job_id == 当前任务
   h. 解压到 project_dir(新建目录,失败 rmtree 整体清掉)
   i. 验 file inventory:逐文件 sha256+size 对比
   j. 重写 JobRecord JSON 文件(用 manifest.job_record 内容)
      ├ set_archive_status(job_id, 'succeeded')
      ├ 写 jobs.r2_artifacts = NULL (**不是 []**)
      └ ⚠️ verified: `r2_artifact_sweeper.py:137` 用 `if db_job.r2_artifacts is None: return True` 选 candidate。
         写 [] 不会触发重推,只 NULL 会触发。alembic 028 column 默认 NULL 即可,无 NOT NULL 约束。
   k. 不同步推 R2 — sweeper 在 300s 内发现 r2_artifacts IS NULL 并推 publish/poster/jianying
                    第一波 download 在 5 min 内可能走 local fallback,可接受(D9)
   l. UPDATE backup_records SET status='restored', completed_at
   m. emit_event('pan.restore.completed')
```

---

## 9. OAuth Web Flow

```
1. Admin 点 "连接百度网盘"
   ↓
   GET /admin/pan/connect
     ├ state_token = secrets.token_urlsafe(32)
     ├ INSERT pan_oauth_states (token, user_id, expires_at=now+10min)
     └ 302 → openapi.baidu.com/oauth/2.0/authorize?
              response_type=code, client_id={appkey}, redirect_uri, scope=basic,netdisk, state

2. 用户在百度页面授权 → Baidu 302 → /admin/pan/callback?code=...&state=...

3. GET /admin/pan/callback
     ├ 验 state_token 存在 + 未过期 + 属于当前 admin
     ├ DELETE pan_oauth_states WHERE token=...
     ├ POST openapi.baidu.com/oauth/2.0/token (grant_type=authorization_code)
     ├ response: {access_token, refresh_token, expires_in}
     ├ Fernet encrypt + UPSERT pan_credentials
     └ 302 → /admin/pan/dashboard
```

**Refresh 后台任务**(每 6h,`execute_pan_token_refresh`):

```sql
SELECT * FROM pan_credentials 
 WHERE status='active' AND access_token_expires_at < now() + interval '24 hours';
```

For each:
1. decrypt refresh_token(旧值)
2. POST oauth/2.0/token (grant_type=refresh_token, refresh_token=旧值)
3. response 包含**新** `access_token` + **新** `refresh_token` + `expires_in`
   ⚠️ Baidu Pan refresh_token **每次都轮换**,旧 refresh_token 在此次请求成功后立即失效
4. `UPDATE pan_credentials SET access_token_encrypted=NEW_ACCESS_ENC, refresh_token_encrypted=NEW_REFRESH_ENC,
   access_token_expires_at=now()+expires_in*1s, last_refreshed_at=now()`(单事务,失败回滚)
5. 异常 → status='revoked' + `await notifications_service.dispatch_event(db, event_type="pan_token_revoked", user_id=user_id, payload={"provider": "baidu_pan"})`
   ⚠️ `dispatch_event` 是 keyword-only(`db` 之后全 keyword)。`event_type` 必须先在
   `gateway/notification_dispatch_map.py` 注册 recipe(message template / level /
   action_link 等),否则 `get_recipe()` 返 None 静默丢弃。新增 recipes 见 §3.2。

**并发约束**: refresh 任务全局 sem=1,避免两个 worker 同时拿同一 user 的 refresh_token 触发"双兑换"(第二个 401)。

---

## 10. 调度

| 任务 | 频率 | 文件 |
|------|------|------|
| `pan_archive_scanner` | 每日 03:30 BJT | `gateway/pan/archive_scanner.py` |
| `pan_token_refresh`   | 每 6h         | `gateway/background_task_executors.py:execute_pan_token_refresh` |
| `pan_orphan_cleanup`  | 周六 04:00 BJT | `gateway/pan/orphan_cleanup.py` |
| `pan_stale_reaper`    | 每 30 min     | `gateway/pan/stale_reaper.py`(**新 infra**,Gateway 当前无 reap-stale 调度) |
| `pan_residue_cleanup` | on-demand from stale_reaper | `gateway/background_task_executors.py:execute_pan_residue_cleanup` |

### archive_scanner 选 candidate

```sql
SELECT j.job_id, j.user_id 
FROM jobs j
JOIN users u ON u.id = j.user_id 
WHERE u.role = 'admin'
  AND j.status = 'succeeded'
  AND j.updated_at < now() - interval '30 days'    -- AVT_PAN_AUTO_ARCHIVE_DAYS
  AND NOT EXISTS (
      SELECT 1 FROM backup_records br 
      WHERE br.user_id = j.user_id AND br.job_id = j.job_id 
        AND br.status IN ('uploading', 'uploaded', 'restoring', 'restored')
  )
  AND EXISTS (
      SELECT 1 FROM pan_credentials pc 
      WHERE pc.user_id = j.user_id AND pc.status = 'active'
  )
ORDER BY j.updated_at ASC                          -- 最老的先 archive,deterministic
LIMIT 100;                                         -- 防一夜涌入(实际可见 §16 timeline 评估)
```

**`updated_at` 字段验证**: 必须在 implementation plan T1 step 1 中 verify `gateway/models.py` Job 行 `updated_at: Mapped[datetime]` 存在,且 Studio editing/commit 路径(`gateway/job_intercept.py` 的 commit 处理)真的 `UPDATE jobs SET updated_at=now()`。若未更新,需补丁。否则 30d 阈值会用错误时间戳。

`updated_at` 语义 = JobRecord 任何修改触发(包括 Studio editing/commit),re-edit 自动续 30d。

**`ORDER BY` 索引匹配**: 见 §4.3 `idx_backup_user_status` + `idx_backup_user_job`(NOT EXISTS 用)+ jobs 表 `idx_jobs_user_status_updated`(若不存在,migration 028 加一个 partial: `WHERE status='succeeded'`)。

### orphan_cleanup 算法

```
1. List remote files under /apps/AIVideoTrans/backups/
2. SELECT remote_path FROM backup_records WHERE status IN ('uploaded','restoring','restored')
3. orphans = remote_files - db_paths
4. For each orphan:
     ├ safety guard: path 必须 prefix /apps/AIVideoTrans/backups/
     ├ baidu_pan_client.delete(path)
     └ emit_event('pan.orphan.cleaned', path)
```

### stale-reap

⚠️ **Gateway 当前无 reap-stale 调度**(verified)。本设计**新建** `gateway/pan/stale_reaper.py` 与对应 background task scheduler hook,不是"扩展现有"。

关键设计:reap 时必须区分**有没有过 commit point**(§7 步骤 i 之后 backup_records 是否 'uploaded')。否则会出现"R2/local 已清但 status 被错误回滚到 succeeded → sweeper 试图重推已删的 local"的死锁。

```python
async def reap_stale_pan_jobs():
    """archiving / restoring 卡超 AVT_PAN_TASK_STALE_HOURS(default 4h)→ 区分 commit 前后处理。
    
    必须取 pg_try_advisory_xact_lock 防与还在喘气的 executor 双跑 cleanup。
    """
    
    # --- archiving 卡死 ---
    stuck_archiving = await db.execute("""
        SELECT j.job_id, j.user_id,
               br.id AS backup_id, br.status AS backup_status
          FROM jobs j
          LEFT JOIN backup_records br 
            ON br.user_id=j.user_id AND br.job_id=j.job_id 
           AND br.status IN ('uploading', 'uploaded')
         WHERE j.status='archiving' 
           AND j.updated_at < now() - interval '4 hours'   -- AVT_PAN_TASK_STALE_HOURS
    """)
    for row in stuck_archiving:
        # ⚠️ 必须取同款 advisory lock 才能动这个任务。若原 executor 还活着挂在
        # PG 重试 / 网络重连里,它持着 lock — 我们会被 try-block 阻塞或拿不到
        # `pg_try_advisory_xact_lock`,直接跳过这个任务等下一轮。原 executor
        # 终会自己完成或自己 crash 释放 lock。**避免 reaper 跟 executor 双开 cleanup。**
        got_lock = await db.execute(
            "SELECT pg_try_advisory_xact_lock(:k)", k=hash_user_job(row.user_id, row.job_id)
        )
        if not got_lock.scalar():
            continue  # executor 还活着,下一轮再看
        
        if row.backup_status == 'uploaded':
            # **已过 commit point** — tar 在 pan,数据安全。仅是 post-cleanup 没完成。
            # forward-resolve: 把任务推到 archived,后台 cleanup retry 残留 local/R2
            await set_archive_status(row.job_id, 'archived')
            await enqueue_background_task('pan_residue_cleanup', {'job_id': row.job_id, 'user_id': row.user_id})
            emit_event('pan.backup.archived', job_id=row.job_id, payload={'reason': 'stale_forward_resolved'})
        else:
            # **未过 commit point** — backup 没完成,数据没出 local。安全 rollback。
            await set_archive_status(row.job_id, 'succeeded')
            if row.backup_id:
                await db.execute("UPDATE backup_records SET status='failed', error_message='reaped after 4h' WHERE id=:id", id=row.backup_id)
            emit_event('pan.backup.failed', job_id=row.job_id, payload={'reason': 'stale_reap'})
        # advisory lock 在 xact 结束时自动释放(pg_try_advisory_xact_lock 语义)
    
    # --- restoring 卡死 ---  
    stuck_restoring = await db.execute("""
        SELECT j.user_id, j.job_id, br.id AS backup_id
          FROM jobs j
          JOIN backup_records br ON br.user_id=j.user_id AND br.job_id=j.job_id AND br.status='restoring'
         WHERE j.status='restoring' 
           AND j.updated_at < now() - interval '4 hours'   -- AVT_PAN_TASK_STALE_HOURS
    """)
    for row in stuck_restoring:
        got_lock = await db.execute(
            "SELECT pg_try_advisory_xact_lock(:k)", k=hash_user_job(row.user_id, row.job_id)
        )
        if not got_lock.scalar():
            continue  # 原 executor 还活着
        # Restore 失败 → 必须回 archived(tar 还在 pan)。先清半残 project_dir。
        await safe_rmtree_project_dir(row.job_id)
        await set_archive_status(row.job_id, 'archived')
        await db.execute("UPDATE backup_records SET status='uploaded', error_message='restore reaped after 4h' WHERE id=:id", id=row.backup_id)
        emit_event('pan.restore.failed', job_id=row.job_id, payload={'reason': 'stale_reap'})
```

**新增 background task `pan_residue_cleanup`**:接收 `{job_id, user_id}`,**先取 `pg_try_advisory_xact_lock(hash(user_id, job_id))`**(避免和挂尸 executor 撞),拿不到就 reschedule 10min 后重试;拿到后重试 `shutil.rmtree(project_dir)` + 逐个 `r2_client.delete_object` + UPDATE jobs.r2_artifacts。失败仍 archived 状态(数据安全),周末 orphan_cleanup 兜底。

调度:`stale_reaper` 每 30 min 自我触发一次(`AVT_PAN_STALE_REAP_INTERVAL_MINUTES=30`),在 gateway 启动期初始化 background scheduler 注册。Stage 部署 checklist 加 `assert stale_reaper 已注册`。

---

## 11. Failure 处置矩阵

| 失败点 | 当前状态 | 回滚动作 | 副作用 |
|--------|----------|----------|--------|
| pan 上传中断(commit point 前) | archiving + uploading | `set_archive_status(succeeded)` + backup_records=failed | local/R2 完全不动 |
| pan 上传成功但 backup_records UPDATE 失败 (race at step i) | archiving + uploading,tar 已在 pan | 4h stale_reaper → backup_records=failed → rollback succeeded。tar 成孤儿 → 周六 orphan_cleanup 删 | 无 user 感知,延迟 1 周清孤儿 |
| Local rmtree 失败(commit point 后) | archiving + uploaded | log error,continue 走 R2 删除 → 进 archived。local 残留 → `pan_residue_cleanup` 后台重试 | 短期 local 残留,周末兜底 |
| R2 delete 失败(commit point 后) | archiving + uploaded | log error,jobs.r2_artifacts 仅移除已成功 delete 的 entry → 进 archived。残留 entry → 周末 orphan_cleanup 顺手清 | 短期 R2 残留($/月微乎其微),周末清 |
| set_archive_status('archived') 写崩 | archiving + uploaded | 4h stale_reaper:**forward-resolve** → 推到 archived(因 backup_records 已 uploaded),enqueue pan_residue_cleanup | tar 在 pan 安全,残留延迟清 |
| Restore download 中断 | restoring + restoring,partial /tmp tar | rollback archived,`/tmp` 清理,backup_records=uploaded 还原 | tar 还在 pan,admin 可重试 restore |
| Restore SHA256 mismatch | restoring + restoring,tar 不完整 | rollback archived,**报警**(可能 pan 端损坏 / MITM) | 需人工介入排查 |
| Restore 解压时 disk full | restoring,partial project_dir | rmtree partial,rollback archived | tmp tar 也清掉 |
| 4h 卡死 archiving | 同 stale_reaper 分支(default `AVT_PAN_TASK_STALE_HOURS=4`) | commit-aware:已 uploaded → forward-resolve archived;未 uploaded → rollback succeeded。reaper 必须 `pg_try_advisory_xact_lock` 才能动 | notifications_service 通知 admin |
| 4h 卡死 restoring | 同上 | rollback archived,清 partial project_dir,backup_records 还原 uploaded。同样取 lock | 同上 |
| Token 撤销 (refresh 失败) | pan_credentials.status='revoked' | scanner 跳过该 user,manual endpoint 返 412 | UI 红 banner + dispatch_event 通知 |
| Encryption key 缺失(flag 开启) | startup 拒启动 | 启动失败 | 容器不起来直到 key 补上 |

---

## 12. Events + Observability

### 12.1 新增 6 个 event_type

```
pan.backup.queued       (scanner enqueue / API enqueue 时)
pan.backup.archived     (backup 成功完成)
pan.backup.failed       (任一步失败 + rollback)
pan.restore.queued
pan.restore.completed
pan.restore.failed
pan.orphan.cleaned      (内部事件,不 surface 到 admin UI)
```

### 12.2 同步点(契约测试守)

- `src/services/jobs/events.py:SUPPORTED_EVENT_TYPES` 加 6 个
- `gateway/storage/event_log.py:_DOWNLOAD_EVENT_TYPES` **保留命名**(line 58-59 注释"kept for git-blame continuity"),只是 frozenset 加 6 个 pan.*
- `scripts/r2_observability.py` 加 PAN 事件分组常量 + 渲染
- `tests/test_phase2_download_backend.py` 更新契约测试 set 加 pan.*
- `tests/test_r2_observability.py::test_script_event_vocab_in_sync_with_jobs_events` **必须改**:现有前缀过滤只匹配 `download.* / stream.*`,会漏 pan.* 漂移。扩到三组前缀

### 12.3 r2_observability 扩展输出

```
--- Pan Backup ---
  Backup 触发:       42   100.0%
  Backup 成功率:    100%  (42/42)
  Restore 触发:       3
  Restore 成功率:   100%  (3/3)
  Orphan cleaned:     1
```

---

## 13. 安全考虑

| 维度 | 措施 |
|------|------|
| Token 静态加密 | Fernet 对称,key 在 env,key 不进 git 不进日志 |
| Token 不进日志 | `logs_redactor.py` mask 关键字 `access_token`/`refresh_token`/`appsecret` |
| OAuth CSRF | state token 32B random + 10min TTL + one-shot delete |
| Callback HTTPS | Baidu 强制;Caddy 已经全站 HTTPS |
| Role gate | 所有 `/admin/pan/*` 走 `require_admin`,flag OFF 返 404 |
| 上传源路径 | 守卫:**复用** `gateway/project_cleanup.py:_is_safe_project_dir(path, safe_roots=DEFAULT_SAFE_PROJECT_ROOTS)`(line 90),不要写并行实现 |
| 删除源路径 | 同上,rmtree 前必走 `_is_safe_project_dir` 返 True |
| Encryption key 备份 | 1Password 主拷贝 + 物理冷拷贝(保险箱纸条) |
| key 轮换 | 不在 MVP。未来加 `gateway/pan/token_crypto.py:rotate_key()` 双 key 过渡 |

---

## 14. Testing 矩阵

### 14.1 Unit tests

| 文件 | 覆盖 |
|------|------|
| `tests/test_baidu_pan_client.py` | mock requests:OAuth code 兑换 / refresh / precreate / chunk PUT / finish / list / delete |
| `tests/test_backup_executor.py` | 假 project_dir + mock pan client:tar 生成 + manifest 完整性 + R2 delete 调用次数 + 状态迁移 |
| `tests/test_restore_executor.py` | 反向:SHA256 mismatch → rollback / inventory mismatch → rollback / 正常 round-trip |
| `tests/test_pan_archive_scanner.py` | SQLite fixture 验 candidate 选择条件(role / status / age / pan_connected / 已有 backup_records 排除) |
| `tests/test_pan_orphan_cleanup.py` | 假 pan list + PG fixture,验 orphan 选择 + safety guard(prefix 错的不删) |
| `tests/test_fernet_token_crypto.py` | encrypt/decrypt round-trip,wrong key 失败 |
| `tests/test_pan_oauth_flow.py` | state CSRF / callback code 兑换 mock / 重放攻击拒绝 |
| `tests/test_pan_stale_reap.py` | archiving 卡 4h → reap rollback(commit-aware:uploaded → forward-resolve archived;uploading → rollback succeeded);restoring 卡 4h → rollback archived;**双分支都 mock `pg_try_advisory_xact_lock` 测拿不到锁时 skip** |

### 14.2 Contract guard

`tests/test_pan_event_vocab_in_sync.py`:`SUPPORTED_EVENT_TYPES` ∩ `pan.*` 必须等于 `_DOWNLOAD_EVENT_TYPES`(保留命名)∩ `pan.*`。任一侧漏改 red CI。

`tests/test_r2_observability.py::test_script_event_vocab_in_sync_with_jobs_events` 必须扩 prefix 过滤,从 `download.* / stream.*` 扩到 `download.* / stream.* / pan.*`。否则现有 prefix 过滤逻辑不会发现 pan.* 漂移。

### 14.3 Integration smoke

`scripts/pan_backup_smoke.py`(单独脚本,不进 CI):
- 建 1MB dummy project_dir
- backup → assert pan 上有 tar.gz + 本地空 + R2 空 + 状态 archived
- restore → assert tar 下来 + project_dir 重建 + 状态 succeeded
- diff manifest 文件 SHA256:回环一致

### 14.4 Manual checklist(灰度上线)

- [ ] OAuth flow 通跑:点连接 → Baidu 授权页 → 回 dashboard 显示已连接
- [ ] 配置 mismatch:`AVT_PAN_TOKEN_ENCRYPTION_KEY` 留空 + flag on → gateway CRITICAL 不启
- [ ] 手动 backup 真 1 GB 任务:看 backup_records 行 + pan 真有 tar.gz + 本地+R2 空
- [ ] 验 archive 显示:前端任务列表见 "archived" badge,download 按钮消失
- [ ] 手动 restore 同任务:验 SHA256 校验通过 + 视频能再播
- [ ] 假场景:停掉 Baidu 网络让 refresh 失败 24h → 看 UI 红 banner + notifications 出现
- [ ] 假场景:`docker kill aivideotrans-gateway` 在 backup 中途 → 4h 后 reap 自动 rollback(`AVT_PAN_TASK_STALE_HOURS` 默认 4h)

---

## 15. 工作量估算

| 模块 | 工日 |
|------|------|
| Schema + alembic 028 + status enum 扩展 + 3 表 model | 0.7 |
| OAuth Web Flow + token refresh + Fernet | 2.0 |
| Baidu Pan API client(OAuth 兑换+refresh+分片上传+下载+list+delete+quota) | 2.0 |
| backup_executor + restore_executor + manifest + status_mutator | 2.0 |
| archive_scanner + orphan_cleanup + **stale_reaper(新 infra)** + residue_cleanup | 0.8 |
| Admin UI(dashboard + backups page + 任务列表按钮 + 状态过滤器 + Restore 流程) | 2.5 |
| Events + observability + r2_observability 扩展 + 契约测试更新 | 0.5 |
| Tests(8 unit + contract + smoke) | 1.5 |
| **合计** | **~12.0 工日** |

> 比初版的 10 工日多 2 天:reviewer 指出 frontend 估太薄 + stale_reaper 是新 infra 不是扩展。

---

## 16. 部署 checklist(实施后)

### 16.1 一次性 setup

- [ ] Admin 完成百度开放平台认证 + 应用注册(1-3 工作日审核)
- [ ] 生成 `AVT_PAN_TOKEN_ENCRYPTION_KEY` 并双备份(1Password + 纸条)
- [ ] `.env` 注入 6 个 env vars
- [ ] alembic 028 在生产 PG 跑通(staging 先跑一遍)

### 16.2 部署 gateway(注意 compose recreate 陷阱)

⚠️ 6 个新 env 加进 `docker-compose.yml` 后,直接 `docker compose up -d` 会触发**整 project**变量重新插值 → 所有 service config-hash 重算 → `app` 容器顺带 recreate(见 `feedback_compose_env_file_recreate.md`)→ kill 正跑的 pipeline。

**正确做法**:
```bash
# 1. 先确认无 active pipeline
psql -c "SELECT count(*) FROM jobs WHERE status IN ('running','editing','restoring','archiving');"
# 必须 = 0,否则等任务跑完或停在 succeeded 再继续

# 2. 只 recreate gateway,跳过依赖
docker compose --env-file /opt/aivideotrans/config/.env up -d --no-deps --force-recreate gateway

# 3. 验证 startup_checks 通过
docker logs aivideotrans-gateway 2>&1 | tail -50 | grep -i "validate_pan_backup"
```

### 16.3 灰度

- [ ] OAuth 连接 + dry-run backup 1 个小测试任务(自建 5MB project_dir)
- [ ] 监控:`scripts/r2_observability.py` 看 PAN 事件分组,前 1 周每日检查
- [ ] 30d cron 第一次触发前,提前在 admin 工作台公告"我们将在 X 月 Y 日开始自动归档 30d 未动的任务"

### 16.4 First-time 41 任务回填 — 现实时间线

⚠️ §11.8 上游说 "4-8 小时" 不现实。实际:
- 41 任务 × 平均 2.5 GB ≈ **100 GB**
- 跨境 US Hetzner → 中国百度网盘上行 ~1-3 MB/s(乐观估计,实际个人开发者授权可能限流到 <500 KB/s)
- D11 并发=1
- **总耗时 10-40 小时**,中位数 ~24h

**策略**:
- 灰度 1 周通过后,再开 30d auto-trigger
- 手动 backup 时分批每天 5-10 个,不在一晚跑完
- 实测某任务 backup 耗时超默认 4h → 调大 `AVT_PAN_TASK_STALE_HOURS` 到 6-8h(单 60GB+ 任务可能合法触发)

---

## 17. 长期 follow-up(MVP 之后)

- 多 provider 接入:阿里云盘 / OneDrive / GDrive,实现 `PanProvider` 协议
- Restore 期间 R2 同步重推(可选,如果异步等 sweeper 体验不行)
- 用户级开放(非 admin 也能用):需要付费方案 / 配额管控 / 多租户 token 隔离
- backup 期间断点续传(MVP 失败重新跑全程,不续传)
- 取消正在跑的 backup(MVP 无,reap 4h 兜)
- key rotation 流程

---

## 18. 已锁定的开放问题

| # | 问题 | 决议 |
|---|------|------|
| O1 | 备份是否要同步重推 R2 | ❌ 不,异步 sweeper(D9) |
| O2 | Orphan cleanup 集成 vs 独立脚本 | 独立脚本,周六(D10) |
| O3 | backup 是否需要并发 | ❌ 不需要(D11),全局 sem 1 |
| O4 | OAuth 接入方式 | 完整 Web Flow(D7) |
| O5 | 通知通道 | `notifications_service.dispatch_event` 站内(D13) |
| O6 | 工作量预估 | ~10 工日(§15) |

---

## 19. 验收 Definition

- [ ] §14.4 Manual checklist 全过
- [ ] CI 全绿:8 个 unit test 文件 + 1 个 contract guard
- [ ] 灰度 1 周后:`r2_observability.py` 看 PAN 事件,failure 率 < 5%
- [ ] 30d auto-trigger 第一次触发后,admin 监控 7 天确认没有误删 / 误归档
- [ ] §16 deployment checklist 全勾
