# Pan Backup Implementation — Session Handoff

> **新会话读这个文件,5 分钟后能续上进度。**

**Last session ended:** 2026-05-18(完成 Phase 1 + Phase 2 + 4 个 CodeX 修复)
**Next phase:** Phase 3 — Baidu Pan API Client(10 tasks,~2.2 工日)

---

## 1. 必读文档(按顺序)

1. **本文件** — 交接状态、教训、节奏
2. `docs/plans/2026-05-14-admin-pan-backup-implementation-plan.md` — Phase 3 规范在 "# Phase 3 — Baidu Pan API Client" 段
3. `docs/plans/2026-05-13-admin-pan-backup-design.md` — 设计 spec(已经过 4 轮 review + CodeX 修订)
4. `CLAUDE.md`(项目根)— 项目硬约束:付费 API / 容器部署 / 远端脚本

---

## 2. 当前 git 状态(验证用)

```bash
# 跑这几条确认环境
git log --oneline -5
# 期望(最近 5):
#   4746cc2 fix(r2): sweeper enable uses effective settings (CodeX P2-4)
#   d558658 fix(cleanup): skip R2 parity gate when project_dir already gone (CodeX P2-3)
#   3179192 fix(r2): sweeper publishes legacy jobs with None edit_generation (CodeX P1-2)
#   89308a3 fix(r2): registry path HEAD-check before presign (CodeX P1-1)
#   961882b feat(pan-backup): T2.5 docker-compose + .env.example — 13 pan env vars

git fetch origin
git rev-list --count HEAD..origin/main  # 应为 0
git rev-list --count origin/main..HEAD  # 应为 0
```

如果上面任一不通过,**先停手报告**,不要继续。

---

## 3. 已经完成(不要重做)

### Phase 1 — Schema foundation(commits e120dc1..739bd55,7 tasks)

| Task | Commit | 内容 |
|---|---|---|
| T1.1 | `e120dc1` | alembic 029 — 3 表 + 索引 |
| T1.2 | `b17c5a4` | PanCredentials / BackupRecord / PanOauthState SQLAlchemy models |
| T1.3 | `3cf6ef9` | `SUPPORTED_JOB_STATUSES` + `ACTIVE_JOB_STATUSES` 加 archiving/archived/restoring |
| T1.4 | `2899327` | `_CLEANUP_PROTECTED_STATUSES` 加 archiving/restoring |
| T1.5 | `6e1173e` | frontend `JobStatus` 扩 + label map + statusMap + ApiJobStatus union |
| T1.6 | `865d24b` | contract guard:Python ↔ TS status vocab 同步 |
| T1.7 | `739bd55` | status badge tone 映射(archiving=ochre / archived=muted / restoring=ochre) |

### Phase 2 — Token Crypto + Config(commits 1e885d0..961882b,5 tasks)

| Task | Commit | 内容 |
|---|---|---|
| T2.1 | `1e885d0` | `GatewaySettings` 加 13 个 pan/baidu 字段 |
| T2.2 | `63ad097` | `validate_pan_backup_config` + 接入 main.py lifespan |
| T2.3 | `0757526` | `gateway/pan/__init__.py` + `token_crypto.py`(Fernet) |
| T2.4 | `7cd2f7a` | logs_redactor 加 4 个 mask 关键字(实际文件在 `src/services/jobs/logs_redactor.py`,gateway 有 loader) |
| T2.5 | `961882b` | docker-compose.yml + .env.example 各 13 个 env vars |

### 顺手修的 4 个 CodeX 找到的 R2/cleanup bug(commits 89308a3..4746cc2)

| Issue | Commit | 内容 |
|---|---|---|
| P1-1 | `89308a3` | Registry path HEAD-check before presign(download + stream)— 防 R2 已删但 registry 还指向时给用户 302→404 |
| P1-2 | `3179192` | sweeper 处理 legacy JSON `edit_generation=None`(default → 0)+ 顺带 fix SQL WHERE 的 IS NULL 漂移 |
| P2-3 | `d558658` | cleanup parity gate 跳过 disk-gone ghost row,让它们能 flip 到 purged |
| P2-4 | `4746cc2` | sweeper `_is_enabled()` 用 `storage.backend_router.is_r2_enabled()` 而非 raw env,respect startup downgrade |

**统计:** 16 commits / ~85 new tests / 0 regression / 0 contamination。

---

## 4. Phase 3 任务清单(下个会话要做的)

读 plan 文件 `# Phase 3 — Baidu Pan API Client` 段(line ~1300+),10 个 task:

| Task | 主题 | 工日 | 复杂度 |
|---|---|---|---|
| T3.1 | `PanProvider` Protocol + 空 `BaiduPanClient` 骨架 | 0.2 | 低 |
| T3.2 | OAuth code exchange | 0.2 | 中 |
| T3.3 | Refresh token(注意 Baidu 每次轮换 refresh_token) | 0.2 | 中 |
| T3.4 | list + get_quota | 0.2 | 低 |
| T3.5 | delete + idempotent on 404 | 0.2 | 低 |
| T3.6 | **4MB 分片上传**(precreate + chunked PUT + finalize)| 0.5 | **高** |
| T3.7 | **Read-back probe**(HEAD + Range GET 64KB)— 第 3 道闸门 | 0.3 | **高** |
| T3.8 | Download(streaming + sha256)| 0.2 | 中 |
| T3.9 | 集成 smoke(skipped in MVP) | 0 | 低 |
| T3.10 | Phase 3 close — 全 test 跑 + batch push | 0.2 | 低 |

Plan 文件里每个 task 都有 verbatim 测试代码 + 实现代码,subagent 直接抄就行。

---

## 5. 执行节奏(这次摸索出来的最佳实践)

### 5.1 启动 subagent-driven-development skill

```
/superpowers:subagent-driven-development
```

把 plan 路径作为 args 传进去,告诉它从 T3.1 开始。

### 5.2 每个 task 的标准流程

```
1. Pre-flight(必做)
   - git fetch origin
   - git rev-list --count HEAD..origin/main  # 应为 0
   - git log --since="1 hour ago" --oneline | wc -l  # 看 smart MVP 是否还在 push
   - 目标文件是否 dirty(应该 clean)

2. Dispatch implementer subagent(sonnet model,5-15 min)
   - Prompt 包含 verbatim 任务规范 + 强制 staged-diff guard

3. Review(如果代码是 plan verbatim,可以 skip)
   - 否则:spec-reviewer subagent → code-quality-reviewer subagent

4. Update TodoWrite + 进下个 task
```

### 5.3 ⚠️ 必须包含的 staged-diff guard(prompt 模板)

每个 implementer subagent 的 prompt 末尾**必须**写:

```
## Step N: CRITICAL — Pre-commit staged-diff guard

git add <explicit files only — NEVER 'git add -A' or 'git add .'>

git diff --cached --name-only
# 应该只有你这个 task 改的文件

git diff --cached <main file> | grep -E "^\+" | head -30
# 应该只看到你的添加。
# 任何 source_*, smart_*, disk_resize_*, edit_generation 等 unrelated 行 →
# 立即 git reset HEAD <file> + 报告 BLOCKED 给 controller。
```

**理由:** 上次 T1.2 翻车原因 = `git add gateway/models.py` 把 voice library 的 dirty
changes 一起 stage 了,subagent 没做 staged-diff 验证就 commit。**这道闸门挡住后续所有 task**。

### 5.4 不要做的事

- ❌ 单 task push(攒到 phase end batch push 减少跟 smart MVP rebase)
- ❌ 创建 worktree / 新分支(项目硬约束:直接 main)
- ❌ `git add -A` / `git add .`(全部用 explicit file path)
- ❌ 改 plan 文件(plan 已经被 4 轮 review + CodeX 锁定;Phase 3 task 代码 verbatim 即可)
- ❌ 自己跑 live PG migration(Windows 本地无 docker;静态验证用 alembic ScriptDirectory + AST parse)
- ❌ 创建 worktree(本会话项目硬约束)

### 5.5 模型选择

- **Sonnet** 跑 T3.1-T3.5 / T3.8-T3.10(单文件 + 完整 spec)
- **Opus** 跑 T3.6 / T3.7(复杂度高,涉及 multipart 状态机 + dual checksum)

---

## 6. Phase 3 特殊注意点

### 6.1 Mock requests 库的模式

T3.2-T3.8 都需要 mock `requests.post` / `requests.get`。pattern:

```python
def mock_post(url, data=None, params=None, **kw):
    class R:
        status_code = 200
        def json(self): return {'access_token': 'fake', ...}
        def raise_for_status(self): pass
    return R()

monkeypatch.setattr(requests, 'post', mock_post)
```

参考 plan T3.2 的完整 test 代码。

### 6.2 真实 Baidu Pan 凭据

用户已经申请好了:`F:\AutoVideoTrans\新建文件夹\百度网盘.txt` 里有 AppKey/SecretKey/SignKey。

**绝对不要在代码或 test 里硬编码这些值**。所有 test 用 mock。真实凭据**只在生产 `.env` 里**,通过 `AVT_BAIDU_PAN_APPKEY` / `AVT_BAIDU_PAN_APPSECRET` 注入。

### 6.3 T3.6 分片上传的状态机

最复杂的 task。分 4 个子 method:
- `_chunk_file(path, chunk_bytes)` — 流式 yield (idx, bytes)
- `_compute_chunk_md5s(path, chunk_bytes)` — 返 (per-chunk md5 list, file-level md5)
- `_precreate(remote_path, size, chunk_md5s, access_token)` — 拿 uploadid
- `_upload_chunk(...)` — 单片 PUT
- `_create_finalize(...)` — 合并 + 返服务端 md5

`upload()` public method 编排这些。Plan T3.6 有完整 verbatim 代码。

### 6.4 T3.7 三道闸门(对应 design spec §7)

这是 commit point 的 critical 验证:

```
h1. size match    — HEAD remote = local size (防截断)
h2. md5 match     — finish API 返的 server_md5 == 本地 md5(防内容损坏)
h3. read-back probe — HTTP Range GET 末尾 64KB,sha256 比对(防"finish 报告 md5 但实际存了别的对象")
```

任一闸门失败 → rollback succeeded + backup_records=failed,**不删 local/R2**。
全过 → 才算 commit point ready。

---

## 7. 并行 work track 的情况

读 plan 时**当时**(2026-05-18)的状态:

- ✅ **Voice library track** — 完全收口(028 migration + models.py UserVoice 列都 commit 了)
- ⚠️ **Smart MVP track** — Codex 40 round 仍偶发 push,但节奏从"1h 8+ commits"降到"1h 2-3 commits"。撞车风险低但非零。
- ⚠️ **Admin disk track** — 5 dirty files 在工作树,**不动 pan-backup 任何目标文件**

每 task 启动前重新看 1h 内 commit 数,>0 警惕,撞 line conflict 就 abort 报告。

---

## 8. 如果发现 plan 错了怎么办

Plan 已经过 4 轮 review,但实施中可能撞到 reality 漂移(比如某个 helper 不存在 / 文件路径错 / pattern 跟现有 codebase 冲突)。规则:

- **小漂移**(spec 路径错 / 接口名小调整):subagent 当场适配,在 DONE_WITH_CONCERNS 里报告实际情况。controller 在 review 时确认。

- **大漂移**(整个架构假设错 / 必须改 plan):subagent 报告 BLOCKED + 详细描述。controller 决定是改 plan 还是 abort task 重新规划。

历史上的小漂移例子(已修):
- T2.4 plan 说改 `gateway/logs_redactor.py`,实际文件在 `src/services/jobs/logs_redactor.py`,gateway 有 loader。subagent 适配后正常完成。

---

## 9. 文件位置速查

```
gateway/pan/
├── __init__.py             # 已存在(T2.3)
├── token_crypto.py          # 已存在(T2.3)
├── provider_protocol.py     # T3.1 将创建
└── baidu_pan_client.py      # T3.1-T3.8 创建并扩展

tests/
├── test_baidu_pan_client.py # T3.1 创建并扩展
```

设计 spec 里规划的其它 `gateway/pan/*` 模块(manifest / status_mutator / archive_scanner / orphan_cleanup / stale_reaper / auth) 是 Phase 4-8 的事,Phase 3 不碰。

---

## 10. 开场 prompt 模板(粘贴到新会话)

```
继续 admin pan backup implementation,从 Phase 3 开始。

读 docs/plans/2026-05-18-pan-backup-session-handoff.md 全文,验证 git
state(§2),然后用 subagent-driven-development skill 跑 Phase 3 第一个
task T3.1。每个 task 用上次摸索出的标准流程(§5),记得 staged-diff guard。

工作目录已经在 main 分支,不要开 worktree。
```

End of handoff.
