# APF P0 部署 + 网关事故复盘 / 交接（2026-06-11，US 生产）

> 一句话：把 **APF P0 匿名预览（休眠）+ 一簇评审修复 + 两个前端重构** 部署到 US 生产
> （`5.78.122.220`）。部署过程中 gateway 因三个"测试绿但 prod 炸"的盲区一度部分故障，
> 已全部定位+修复+对齐。**当前生产健康，APF 开关仍关，用户零感知。**

---

## 1. 部署范围（本次实际上线的内容）

| 组件 | 变更 | 用户可见？ |
|---|---|---|
| frontend (`next`) | ConfirmDialog 替换 window.confirm、`lib/format.ts` 抽取 | **是**（这两个重构生效） |
| frontend (`next`) | APF 匿名预览面板/launcher/api | 否（`NEXT_PUBLIC_ENABLE_ANONYMOUS_PREVIEW=0`，零渲染） |
| gateway | APF 端点、R2 sweeper 匿名豁免、job_terminal_mirror 结算 bypass | 否（`AVT_ENABLE_ANONYMOUS_PREVIEW=false`） |
| app (`src/`) | pipeline 匿名标记、downloadable_keys、jobs 模型 | 否（gated） |
| DB | migration 035：`jobs.is_anonymous_preview` 列 + 3 表 + 分区索引 + sentinel 用户 | — |

**决策：flag 全关上线（"代码先睡着"）。** APF 激活（真实匿名预览、真实付费）是**独立的未来一步**，
本次不做（e2e 冒烟未跑、会真实调付费 API）。

> ⚠️ **重要纠正**：原以为"flag 关就免 migration"——**错**。合并的 `Job` ORM 模型新增
> `is_anonymous_preview` 列、每次 job 查询都 SELECT 它，与 flag 无关。所以 **migration 035 是
> 必须的**，哪怕功能休眠。

---

## 2. 部署执行（机制）

走 SOCKS 跳板（`127.0.0.1:11080`）+ 底层 helper（`Deploy-Via-154` 链可能因缺 uv 卡死，见
[[deploy_experience]] 踩坑 14）：

```bash
# 打包（commit-only，永不 tar 工作树）
git archive HEAD frontend-next gateway src -o /d/Claude/temp/avt-deploy.tar.gz
# 上传（//tmp 双斜线绕 MSYS）
PYTHONIOENCODING=utf-8 python D:/daili/scripts/sftp_over_socks_upload.py 127.0.0.1 11080 <local> 5.78.122.220 22 root <key> //tmp/avt-deploy.tar.gz
# 远端解压 + app restart（bind-mount src）+ 后台 build gateway/next（nohup+log poll，paramiko 20s 会杀 build）
PYTHONIOENCODING=utf-8 python D:/daili/scripts/ssh_over_socks_command.py 127.0.0.1 11080 5.78.122.220 22 root <key> "<cmd>" --timeout <N>
```

部署前**硬门已查**：`select count(*) from jobs where status='running'` = 0（force-recreate 会杀在跑的 pipeline）。

---

## 3. 事故：gateway 部分故障（三个根因，已全修）

部署后 gateway 一度崩/job 查询报错（前端、app 正常）。三个独立根因：

### 3.1 `ModuleNotFoundError: No module named 'src'` → gateway 重启循环
- **根因**：gateway 容器 WORKDIR=`/opt/gateway`，各模块 `sys.path.insert` 只加 `app/src`（够
  `from services.X`，17 个老文件的约定）；但 **APF 网关模块 + 共享 `src/services/*` 模块内部**
  用 `from src.services.X`，需要 **`app/`（src 的父目录）** 在 sys.path 上。缺 → 崩。
- **修法**（两步，最终靠后者）：
  1. `abbf2247`：把 7 个 gateway APF 文件 `from src.services.X` → `from services.X`（治标——
     共享 `src/services` 模块内部仍 `from src.services.X`，还会再炸）。
  2. **`fde9b48`：`docker-compose.yml` gateway `environment: PYTHONPATH: "/opt/aivideotrans/app"`**
     （治本——app/ 上 path，`src.services.X` 处处可解）。生产先用 `docker-compose.override.yml`
     临时桥接，**最终已把主 compose 部署上去、删掉 override**（见 §4）。

### 3.2 `column jobs.is_anonymous_preview does not exist`
- **根因**：见 §1 纠正——ORM 模型要这列，flag 关也要 migration。
- **修法**：跑 `alembic upgrade head`（034→035）。

### 3.3 migration 035 半应用（sentinel INSERT 失败）
- **根因**：`users.created_at`/`updated_at` 是 NOT NULL 无 default，但 035 的 sentinel INSERT 漏了
  这两列 → `NotNullViolationError`。DDL（列+3表+CONCURRENTLY 索引）在 autocommit_block 前已提交，
  但 alembic 停在 034、sentinel 未插 = **半应用态**。
- **修法**：
  - `fb3a0fd`：INSERT 补 `created_at/updated_at = now()`（保证未来 fresh DB 不再失败）。
  - 生产手工补 sentinel（`placeholder_anon_preview_no_login` 占位 hash，sentinel 永不登录）
    + `alembic stamp 035_anonymous_preview` 对齐。
  - ⚠️ **半应用态别盲目 re-run**：035 的 `op.create_table` 无 `IF NOT EXISTS`，重跑会"表已存在"再炸。

---

## 4. 最终生产状态（US `5.78.122.220`）

- 三容器全 **healthy**：gateway / next / app（+ postgres / caddy / cloudflared / disk-resize-helper）
- 远端 compose：**root + app 两份均 = main 版**（sha256 与 repo `HEAD:docker-compose.yml` 一致，
  内联 PYTHONPATH + APF env 默认关），无 override
  - ⚠️ **纠正（2026-06-11 CodeX 复核发现）**：本文初版此处写"远端 compose = main 版、override 已删"
    ——**不准确**。当时 main 版 compose 只落在 `/opt/aivideotrans/app/`，**规范入口
    `/opt/aivideotrans/docker-compose.yml` 仍是 Jun 4 旧版**（无 PYTHONPATH / APF env /
    Paddle build args）；gateway/next/app 从 app compose 创建、postgres/caddy 从旧 root
    compose 创建——同一 project 被两份文件分治，即
    [US_HOST_PRODUCTION_DEPLOYMENT.md](../deployment/US_HOST_PRODUCTION_DEPLOYMENT.md)
    "Known Bad Pattern" 的复发。若有人按文档从 `/opt/aivideotrans` 跑 `up -d --build`，
    gateway 会丢 PYTHONPATH 回到 crash-loop。**已修复**：root/app 两份均对齐 repo main
    （旧 root 备份 `docker-compose.yml.bak-rootdrift-20260611`），规范入口 `config -q` 校验通过。
  - 容器 label（`com.docker.compose.project.config_files`）仍指 app 路径，要等下次从 root
    入口 force-recreate 才收敛——内容已一致，`up -d` 对 gateway/next/app 是 no-op，**不必**
    为洗 label 专门 recreate（会杀 in-flight pipeline）。
- DB：alembic **= 035 (head)**；`is_anonymous_preview` 列 + 3 表 + `ix_jobs_anon_preview_status`
  索引 + sentinel 用户（`anonymous-preview@system`）全在
- **APF 开关：关**（`AVT_ENABLE_ANONYMOUS_PREVIEW=false` / `NEXT_PUBLIC_ENABLE_ANONYMOUS_PREVIEW=0`，
  secret 未配）→ 用户零感知，零真实付费
- `curl https://aitrans.video/` → 200

---

## 5. 本次相关 commits（main）

部署/事故修复：
```
fde9b48 fix(deploy): gateway compose 加 PYTHONPATH=/opt/aivideotrans/app
fb3a0fd fix(migration): 035 sentinel INSERT 补 created_at/updated_at
abbf2247 fix(gateway): APF 网关模块改用 from services.X 约定
```
本会话早先（评审/合并）：`1b602a7a` 合并 APF / `566fcc1d` F18 隔离 / `d48222ad` D43 守卫 /
`76f7e5a4` 评审修复簇 / `3ae563b9` migration invalid-index guard。

> 并发漂移：部署期间另有会话往 main 推了前端 commit（`8049df7c`/`d28489ee`/`2451720f`）。
> 本次打包在它们之前，**这次部署的前端 = 打包那刻的 main**，不含那几个——归对应会话部署。

---

## 6. 剩余事项 / APF 激活 checklist（未来）

激活 APF（让真实用户能用、会真实付费）前置：
- [ ] e2e 部署冒烟（真上传→真预览，真实付费 ~¥0.5/次，**用户显式触发**）
- [ ] 远端 `.env` 配 `AVT_ANONYMOUS_PREVIEW_HASH_SECRET`（≥32 字节；缺则 startup_checks 降级把 flag 关）
- [ ] 双端 flag 开：`AVT_ENABLE_ANONYMOUS_PREVIEW=true` + `NEXT_PUBLIC_ENABLE_ANONYMOUS_PREVIEW=1`
      （后者是 **build-time** baked，改了要 **重建 next 镜像**）
- [ ] §5 成本确认（global cap 500/天 ≈ ¥250/天上界）+ admin 开关族
- [ ] 改 `.env` 后用 `up -d --force-recreate`（`restart` 不重读 env）

---

## 7. 部署教训（四条，已入记忆 [[feedback_apf_deploy_incident]]）

1. **gateway 需要 `PYTHONPATH=/opt/aivideotrans/app`**（共享 `src.services.X` 导入）。改 import 前缀治标，
   给 app/ 上 path 才治本。
2. **"flag 关 ≠ 免 migration"**：合并的 ORM 模型加了列 → migration 必须，哪怕功能休眠。部署 code-only 前先
   diff 模型列 vs 远端 schema。
3. **migration 数据 INSERT 要核 prod 的 NOT NULL 列**（跨 host default 差异让"测试绿"在 prod 炸）；半应用态
   靠 `stamp` + 手工补完，别 re-run。
4. **compose 改动必须落到 root 入口 `/opt/aivideotrans/docker-compose.yml`**（唯一生产入口，见
   [US_HOST_PRODUCTION_DEPLOYMENT.md](../deployment/US_HOST_PRODUCTION_DEPLOYMENT.md)）。只更新
   `app/` 下那份 = Known Bad Pattern 复发：project 被两份文件分治，下次有人从 root 跑 `up -d`
   就把修复全部回滚。部署后必须用 `docker compose ls --all` + 容器 label 验证入口唯一。

通用：`git archive HEAD`（永不 tar 工作树）、`//tmp` 双斜线、nohup 后台 build + log poll、部署前查 in-flight、
`up -d` 会重插值整 project 可能牵连 app。详见 [[deploy_experience]] / [[feedback_compose_env_file_recreate]]。

---

## 8. 回滚参考（如需）

- compose：`/opt/aivideotrans/app/docker-compose.yml.bak-20260611-apf` 是部署前快照（app 侧）；
  `/opt/aivideotrans/docker-compose.yml.bak-rootdrift-20260611` 是 root 入口对齐前的 Jun 4 旧版。
- gateway/next 旧镜像：本次 rebuild 已覆盖 `:latest`，旧镜像为 dangling（`docker images -a`）。
- DB：035 的 downgrade 在 `gateway/alembic/versions/035_anonymous_preview.py`（drop 3 表 + 列 + 索引 +
  sentinel，CONCURRENTLY 安全）；但**回滚 DB 会再次触发 §3.2**（gateway 模型仍要这列），除非同时回滚 gateway 代码。
