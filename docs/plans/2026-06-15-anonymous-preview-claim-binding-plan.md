# 匿名预览 → 登录认领（Claim Binding）实施方案

- **日期**：2026-06-15
- **作者**：Claude Code（待项目主 + CodeX 审）
- **状态**：**已实现 + 5-lens 对抗复审收口**（设计 v3.1 = CodeX 三轮 14 点；实现见下「实现状态」，默认 OFF 待项目主灰度）

## 实现状态（2026-06-15，分支 claude/anon-claim-binding）

T1-T4 全部实现，默认 OFF inert：
- **T1 数据层**：migration 040（`anonymous_preview_records.claim_user_id` UUID NULL + 索引）+ ORM 列。alembic 单 head。
- **T2 端点**：`POST /gateway/anonymous-preview/claim`（CSRF→admin gate→显式 None→401→cookie no-op→限频→单条 `UPDATE...RETURNING` 绑 record→session 锁→失败回滚无半状态）。admin 旗 `anonymous_preview_claim_enabled` StrictBool 默认 False（前端 admin 页 4 处同步）。**记录绑定加 `status == 'ready_for_mode'` 过滤**（只绑可认领的 ready record，排除 block 死 record；复审多 lens 收敛）。
- **T3 sweeper**：无代码改动（保留靠 expires_at GREATEST 延长，sweeper 凭 expires_at 自然跳过）。
- **T4 前端**：`lib/api/claim.ts`（fire-and-forget）+ `post-auth-redirect` 登录后认领 hook + 试用面板 ready 设 localStorage hint。
- **测试**：`test_anonymous_preview_t10_claim.py`（35 项，含 4 项真 SQL 覆盖——捕获 handler 真实 statement 编译到 PG dialect 验 jsonb_set/greatest/coalesce/条件 WHERE/RETURNING + 真 SQLite 引擎验越权/status/expiry 过滤真语义）+ `test_anonymous_preview_claim_admin_sync_guard.py`。
- **5-lens 对抗复审**（money-redline/security-overreach/sql-concurrency/inert-availability/test-adequacy）：4 lens SHIP；test-adequacy 的 HIGH（真 SQL 零覆盖）+ MEDIUM（越权只 mock）已修；其余全 LOW（限频 fail-open 等有意取舍，docstring 记）。回归 908 passed / 0 新失败（3 失败为预存基线，stash 验证过）。
- **激活前 gate（项目主）**：merge 分支 + 生产 alembic upgrade 040 + 真金小额 E2E（匿名预览→登录→认领→转完整）+ CodeX 终审 → 才翻 `anonymous_preview_claim_enabled`。

---
- **前置链**：
  - `docs/plans/2026-06-01-anonymous-preview-funnel-ux-plan.md` §11（登录与任务认领设计）、§17.1/§17.2（登录引导/登录墙文案）
  - `docs/plans/2026-06-10-apf-anonymous-preview-vertical-slice-plan.md` §3（明列「claim token 消费/绑定」= Phase 4 后置）、AD-4（匿名 session 表「给 Phase 4 claim 绑定留行」）
  - `docs/plans/2026-06-02-apf2-anonymous-intake-contract.md`（C22：`claim_token_placeholder` 不签发不消费）
- **红线依赖**：`feedback_terminal_state_single_entry`（终态结算单一入口）、CLAUDE.md 付费 API 硬约束、匿名标记 server-only 不可提权

---

## v3.1 修订记录（CodeX 第三轮审核 2026-06-15，4 点已纳入）

CodeX 确认 v3 已落实前两轮 10 点、汇报无夸大；补 4 处实现级收口：

1. **【P1】消除 session-claimed-但-record-未绑的半状态**（§5.2）：v3 是 `SELECT eligible` → update session → 逐条 update record，事务间 record 若被 sweeper/并发删改 → 半状态。改为：record 绑定用**单条 `UPDATE anonymous_preview_records ... RETURNING preview_id`**（带 `expires_at>now AND (claim_user_id IS NULL OR =user)` 条件，原子、无 SELECT 窗口）；返回空 → rollback（不碰 session）；session 认领放在 record 绑定**之后**，失败也 rollback → 绝无半状态。
2. **【P2】`jsonb_set` 处理 `audit IS NULL`**（§5.2）：`audit` 列 nullable，`jsonb_set(NULL,…)` 仍是 NULL。改为 `jsonb_set(COALESCE(audit,'{}'::jsonb), '{claimed_at}', to_jsonb(:now))`。
3. **【P2】统一"无可认领/他人已认领 → 200 no-op"（防探测）**（§5.2/§5.3/§8）：v3 残留 409 分支与"eligible 空→200"语义不一致（record 被他人 claim 会先令 eligible 空→走 200）。**拍板：统一 200 no-op `{claimed:false}`，不返回 409**——零跨用户信息泄露；防误绑他人的安全保证由条件 `UPDATE`（`claim_user_id IS NULL OR =user`）维持，与返回码无关。文档+测试一致去 409。
4. **【P3】`anonymous_preview_claim_enabled` 默认 OFF**（§10/§11 D6）：虽不碰钱/clone，但它延长媒体保留 + 新增认证写端点 → 默认 OFF，admin 灰度确认后再开（不再建议默认 on）。

---

## v3 修订记录（CodeX 第二轮审核 2026-06-15，5 点已逐条核实并纳入）

CodeX 确认 v2 对前 5 点采纳准确（尤其 `stored_upload_path` 缺口已补），再补 5 处（3 P1 必改 + 2 P2，均经 file:line 复核）：

1. **【P1】SQL 恒真条件**（§5.2）：v2 伪代码 `WHERE session_id_hash = session_id_hash` 是**列=列恒真**，照抄会错绑多条 session。改为限定列 + bind 参数：`WHERE anonymous_sessions.session_id_hash = :session_id_hash`。
2. **【P1】并发胜出用 `RETURNING` 不用 `rowcount`**（§5.2/§5.3）：既有匿名 create 已踩过 rowcount 坑、明确改 `UPDATE ... RETURNING` + `.first() is not None`（[anonymous_preview_api.py:1392-1411](../../gateway/anonymous_preview_api.py:1392)，注释"不依赖 asyncpg 的 rowcount"）。`/claim` 的认领锁必须同款 RETURNING。
3. **【P1】先确认 eligible record 再认领 session**（§5.2）：v2 先 UPDATE `session.claim_user_id` 再查 record，若 session 在但无未过期可认领 record，会把 session 误标 claimed（应 no-op）。改为**先查 eligible records，空→200 no-op（不写 session）**；非空才在同一事务原子认领。
4. **【P2】`/claim` 必须 CSRF / same-origin**（§5.1/§5.3/§8）：这是用 `avt_session + avt_anon` 两个 ambient cookie 做状态变更的 POST。既有 `/upload`/`/create` 都先调 `require_same_origin_state_change`（[anonymous_preview_api.py:379](../../gateway/anonymous_preview_api.py:379)/[:1230](../../gateway/anonymous_preview_api.py:1230)，helper 在 `gateway/csrf.py`）。`/claim` 必须同样前置，失败 403。
5. **【P2】`stored_upload_path` 转完整须路径 + 哈希校验**（§6.5）：用完整源时钉死——路径存在、**位于匿名上传根目录内**（防遍历/污染）、**不是 `teaser_path`**、文件 hash **匹配 `record.source_hash`**（防陈旧/替换）。

---

## v2 修订记录（CodeX 第一轮审核 2026-06-15，5 点已逐条核实并纳入）

CodeX 确认现状核实准确、Model A「不迁移 job / 不触结算 / 不触发 clone」三红线正确，并补 5 处硬约束（均经 file:line 复核确认）：

1. **`record.session_id` 派生**（§5.2）：record 的 `session_id` **不是** `avt_anon` 的 hash 本身，而是 `hash_scope_key("sess:" + session_id_hash)`（[anonymous_preview_api.py:129](../../gateway/anonymous_preview_api.py:129)）。`/claim` 反查 record 必须复用同一派生，否则恒查不到（2026-06-11 冒烟同款坑）。
2. **`/claim` 认证不能只 `Depends(require_auth)`**（§5.1/§5.3.5）：`require_auth` 在 `auth_required=false` 时返回 `None` 不抛 401（[auth.py:126](../../gateway/auth.py:126)）。端点必须显式 `if user is None: 401`。
3. **转完整必须用 `audit.stored_upload_path`（完整源），不是 teaser**（§6.5/§6.1）：匿名 create 送进 Job API 的 `source`/`source_ref` 是 **teaser**（[anonymous_preview_api.py:1433](../../gateway/anonymous_preview_api.py:1433)/[:1523](../../gateway/anonymous_preview_api.py:1523)）；完整原始上传在 `audit["stored_upload_path"]`（[:528](../../gateway/anonymous_preview_api.py:528)）。认领后转完整必须读 `stored_upload_path`，且**保留期延长必须覆盖该文件**（它在 gateway 上传目录、会被 sweeper 清）。
4. **record owner 列 = 必做 migration**（§4）：`AnonymousSession.claim_user_id` 适合做一次性认领锁，但「按 user 列出/继续使用已认领预览」应在 `anonymous_preview_records` 上有**正式 owner 列 + 索引**（migration 040），不靠 JSON `audit`。
5. **DB 写失败 ≠ 200 no-op**（§5.3.7）：无 `avt_anon` / 无可认领记录 → 200 no-op；DB **写**失败 → **503 / retryable**，绝不让用户误以为已绑定而实际丢失。

---

## 0. 一句话

匿名用户本地上传 → 看完免费预览 → 注册/登录后，凭浏览器既有的 `avt_anon` HttpOnly 会话 cookie 把那次匿名预览的 **source metadata / 所选方案 / 合规结果 / 价格估算 / consent 审计** 原子绑定到新账户，作为匿名→注册转化漏斗的桥；绑定本身**不迁移哨兵 job 所有权、不碰终态结算、不触发任何付费克隆**。

---

## 1. 现状核实结论（Step 1，已 reconcile 项目主记忆 vs 代码）

> 结论 = **② 仅占位，消费/绑定从未实现**。项目主"出过方案并实施了登录认领"的记忆里——**方案（设计）确实存在**（06-01 §11），但**实施（消费/绑定）不存在**。最可能被记忆混淆的是另一个**已实现但不同**的特性：智能版 **preview→full convert（同一登录用户复用自己的预览）**。

逐条证据（file:line）：

| 事实 | 证据 |
|---|---|
| **占位列存在但只声明** | `gateway/models.py:1800` `AnonymousPreviewRecord.claim_token_placeholder`（注释「Phase 4 留行」）；`gateway/models.py:1769` `AnonymousSession.claim_user_id`（注释「left NULL until Phase 4 … claims their anonymous session」）；migration `gateway/alembic/versions/035_anonymous_preview.py:162` |
| **token 只生成、不消费** | 生成：`gateway/anonymous_preview_api.py:1494` `fresh.claim_token_placeholder = _secrets.token_urlsafe(16)`（create 流程内）。透传：`anonymous_preview_record_store.py:93/135`。adapter/intake 处处置 None：`src/services/anonymous_preview_intake.py:227/695`、`anonymous_preview_backend_adapter.py:639`。**全仓无任何读取该 token 做绑定的端点/逻辑**（`src/` 侧消费扫描仅命中 dataclass 默认 None）。 |
| **`claim_user_id` 从未被写真值** | `gateway/anonymous_session.py:127` 建会话恒 `claim_user_id=None`；全仓无 UPDATE 该列的代码 |
| **匿名预览 router 无 `/claim` 端点** | `anonymous_preview_api.py` 仅 5 路由：`POST /upload`、`GET /limits`、`GET /{id}/status`、`GET /{id}/stream`、`POST /{id}/create` |
| **gateway 里所有 `claim*` 函数都是别的语境** | `_reset_create_claim_to`/`_abort_create_claim`/`_reset_create_claim`/`_claim_predicate`/`won_claim`（`anonymous_preview_api.py:963/986/1185/1397/1411`）= create 流程抢 `__creating__` 哨兵的乐观锁；`chunked_upload_store.claim_upload`=分片上传认领（用户已知）；`express_voice_cleanup_service.claim_batch`/`voice_calibration_inflight.claim_or_join`=sweeper/校准。**无一是匿名→账户绑定** |
| **`gateway/auth.py` 无任何 token 消费/注册回调认领** | grep `claim\|认领\|绑定\|register.*callback\|after_login` → **0 命中** |
| **git 全历史无认领提交** | `git log --all -i --grep='claim\|认领\|绑定\|attach\|associate\|匿名.*注册'` → 命中全是分片上传 claim闭环、临时音色 cleanup claim-lease、express reservation、`__creating__` 哨兵 asyncpg claim、voice calibration；**无匿名→账户绑定** |
| **匿名 job 永久归属哨兵用户** | `anonymous_preview_api.py:882` `_SENTINEL_USER_EMAIL = "anonymous-preview@system"`；`:1438` submit_job payload `user_id=str(sentinel.id)`、`:1521` PG `Job(user_id=sentinel.id, is_anonymous_preview=...)`；**全仓无任何代码把匿名 job 的 user_id 从 sentinel 改写为真实用户** |
| **两份 plan 明确后置** | 06-10 §3「claim token 消费/绑定：仅生成随机串存入 record 占位（**Phase 4 再接**）」+ 后置清单第 4 项「claim token 消费/登录绑定（Phase 4）」；AD-4「给 Phase 4 claim 绑定留行」「claim 单向升级」 |

**与「convert/reuse」的区分（项目主记忆最可能指的实现）**：
`gateway/preview_reuse_service.py::resolve_preview_reuse` 已实现，但它做的是 **ownership 校验**（`:82` `job.user_id != user_id → REASON_FORBIDDEN`）——**同一登录用户复用自己的智能版预览**生成完整付费任务。它是 *登录用户→自己的预览* 复用，**不是 *匿名→注册账户* 绑定**。两者是相邻但不同的模式：claim binding 解决的是「匿名预览在注册后丢失」的漏斗断点，convert 解决的是「登录用户把预览转完整」。

**匿名 Express 预览（`claude/anon-express-preview`，已 merge `2781948f`，T0-T6）不含 claim/绑定**：T0-T6 是 lane resolver / 配额 / mimo 纵深 / 配额退还，git log 与代码均无认领痕迹。

→ **确为缺口**，进入 Step 2 出方案。

---

## 2. 目标与范围

### 2.1 目标
让匿名预览在注册/登录后**不丢失**——把那次匿名预览的转化相关上下文绑定到新账户，使用户能无缝继续（看更长预览 / 转完整付费 / 保存 / 下载等需登录的动作）。

### 2.2 In-scope
- 一个原子、一次性、防重放、防越权的「认领」机制，把匿名 session + 其预览 record 绑定到刚注册/登录的用户。
- 保留 06-01 §11 列出的全部字段：source metadata / 所选方案 / 推荐方案 / 合规结果 / 媒体分析结果 / 价格估算 / consent 审计。
- 与现有匿名 TTL sweeper / 配额 / 限频 / server-only 匿名标记的安全交互。

### 2.3 Out-of-scope（本期不做，独立后续）
- **哨兵 job → 真实用户的所有权迁移**（见 §5 决策，推荐**不做**）。
- 匿名 express CosyVoice 临时音色 → 真实用户音色库的迁移（见 §6.4，推荐**不做**，转完整时按正常 gated 流程重克隆）。
- 「认领后转完整」的具体计费/抵扣（走正常 create + 既有 `preview_reuse_service`/普通付费流程，**本方案不新增钱逻辑**）。

### 2.4 红线（不可破坏）
- **付费 clone**：认领过程**绝不**触发任何 clone（CosyVoice/MiniMax/MiMo 一律不碰）；转完整若要克隆，走 CLAUDE.md 白名单的「用户显式触发 + gated」路径。
- **终态结算单一入口**（`feedback_terminal_state_single_entry`）：认领**不改写**任何已/将进入终态的 job 的 user_id，**不触碰** `mirror_job_terminal_state` / `settle_*` 路径。
- **匿名标记 server-only 不可提权**：认领端点对客户端夹带的 `is_anonymous_preview` / `user_id` / `job_id` 等字段无条件 strip（沿用 create 路径既有防提权）。

---

## 3. 核心设计决策（带推荐 + 留给项目主/CodeX 的决策点）

### D1. 绑定模型 = **元数据桥（Model A）** ✅ 推荐 / vs 任务升级（Model B）

| | Model A 元数据桥（推荐） | Model B 任务升级 |
|---|---|---|
| 做什么 | 绑定 **record（元数据/consent/估价/合规）** 到用户；哨兵 job 不动 | 把哨兵 job 的 user_id 改写为真实用户，teaser job 变成用户的「正式任务」 |
| 终态结算 | **零触碰**（job 仍哨兵所有、仍走 is_anonymous_preview 跳分钟结算） | 高危：teaser job 可能已终态/已镜像，改 user_id 会破坏 single-entry 的 is_anonymous_preview 跳结算不变量 |
| 产物 | teaser（水印+stream-only+TTL）自然过期；用户经转完整拿真成片 | 把残缺 teaser 当「正式任务」展示，语义错乱（无下载/编辑/导出，受 service_mode 限制） |
| 复杂度 | 低；复用既有 record + `claim_user_id` 列 | 高；要处理 job 状态机、policy 门、settle 重入 |
| 红线 | 干净 | 撞终态结算单一入口红线 |

**推荐 Model A**：teaser 本就是**有意的非交付物**（水印 + stream-only + 24h TTL，见 P3e-3*/匿名 lane）。把它"升级"成正式任务既无价值又危险。认领只做**元数据桥**：保留转化上下文 + 延长保留，用户经**正常 create（新任务、干净结算）**拿真成片。这与既有 `preview_reuse_service`（convert）模式同构、与终态结算红线完全相容。

> **决策点 D1（项目主/CodeX）**：确认采用 Model A。若坚持 Model B（teaser 本身要可见于用户工作台），需单开「哨兵 job 所有权迁移 + 终态结算重入安全」专项，风险显著更高。

### D2. 认领凭证 = **`avt_anon` HttpOnly cookie 作 bearer** ✅ 推荐 / vs 显式 token 下发前端

- **现状**：`claim_token_placeholder` 服务端生成后**从不下发客户端**；`avt_anon`（HttpOnly+Secure+SameSite=Lax，24h TTL）已唯一标识匿名 session 且通过 `session_id` 关联 record。
- **推荐**：**不向客户端暴露任何 token**。登录后同一浏览器仍带 `avt_anon` cookie，认领端点（已认证为新用户）同时读 `avt_session`（新用户）+ `avt_anon`（匿名 session）→ 以 `avt_anon` 的 HMAC hash 查 session → 绑定。HttpOnly 不进 JS，XSS 面最小；天然受 session/record TTL 限界（>24h 回访 session 已过期被 sweep，无可认领=可接受，预览本就没了）。
- **`claim_token_placeholder` 的归宿**：降级为**服务端一次性幂等标记**（认领时校验/消费），或在确认 cookie-bearer 足够后**弃用**（保留列，标 deprecated）。**不**下发前端。

> **决策点 D2**：确认用 cookie-bearer（不下发 token）。备选「显式 token」仅在需要跨浏览器/跨设备认领时才必要——但匿名 session 本就 cookie 绑定单浏览器，跨设备认领无意义。

### D3. 触发时机 = **登录/注册成功后，前端调专用 `POST /gateway/anonymous-preview/claim`** ✅ 推荐

- **不**在注册/登录端点内部隐式认领（保持 auth.py 单一职责、避免登录路径耦合匿名表）。
- 前端检测「本会话有过匿名预览」→ 登录成功后显式调 `/claim`。因 `avt_anon` 是 HttpOnly 前端读不到，需一个**非敏感 UX 提示位**判断是否有预览可认领：
  - 推荐：预览 ready 时设一个**非 HttpOnly 提示 flag**（如 localStorage `avt_anon_preview_pending=<preview_id>` 或非 HttpOnly hint cookie），仅作「是否展示『认领你的预览』入口 / 是否 post-login 自动调 claim」的 UX 判断；真凭证仍是 HttpOnly `avt_anon`。
  - 兜底：登录后**总是**尝试一次 `/claim`（无 `avt_anon` / 无可认领 → 200 no-op，廉价）。
- 文案沿用 06-01 §17.2 按动作的登录墙（「登录后生成完整视频 / 保存任务 / 下载…」），登录成功后顺带认领。

> **决策点 D3**：自动认领（登录后静默绑定）vs 显式「认领」按钮。推荐**自动 + 可选显式入口**（漏斗转化优先；但若产品要让用户"有感知地认领"，可只保留显式按钮）。

---

## 4. 数据模型

**复用既有占位，最小新增**：

- `anonymous_sessions.claim_user_id`（已存在，Phase 4 留行）→ **本方案启用**：认领时原子写真实用户 UUID。是「session 已被谁认领」的单点真源。
- `anonymous_preview_records`（已存在列：`session_id` / `source_type` / `source_hash` / `mode` / `job_id` / `audit` JSONB / `expires_at`）→ 认领时：
  - 经 **`session_id` 反查**（注意：`session_id` = `hash_scope_key("sess:" + session_id_hash)`，**非** cookie hash 本身，见 [anonymous_preview_api.py:129](../../gateway/anonymous_preview_api.py:129)，`/claim` 必须复用同一派生 = CodeX #1）；
  - **延长 `expires_at`**（防 sweeper 在转完整前清掉媒体/行，见 §6.1）；
  - `audit` 追加 `claimed_at`（审计，不改既有 `anonymous_consent`/`retry_chain`）。

- **新增列（必做，migration 040 = CodeX #4）**：`anonymous_preview_records.claim_user_id`（`UUID NULL` + 索引）。这是「按 user 列出/继续使用已认领预览」的**正式 owner 列**——**不靠** JSON `audit.claimed_by`（JSON 无索引、不适合做查询/owner 真源）。`AnonymousSession.claim_user_id`（已存在列）做**一次性认领锁**，record 的 `claim_user_id` 做 **owner + 查询**，二者分工。
- `claim_token_placeholder`：按 D2 降级为一次性幂等标记或弃用（保留列，标 deprecated）。
- **无新表**。无新付费/账本表。

---

## 5. 认领流程（端点契约 + 不变量）

### 5.1 端点
```
POST /gateway/anonymous-preview/claim
  CSRF：前置 require_same_origin_state_change(request)，失败 403（v3 #4，与 /upload /create 一致）
  认证：自取 current_user（避免 require_auth 在 auth_required=false 返 None 的坑，v2 #2）
        user is None → 显式 401（绝不在匿名态认领）
  cookie：读 avt_anon（HttpOnly）
  body：{}（无客户端可信输入；任何夹带 user_id/job_id/is_anonymous_preview 一律 strip）
  返回：200 { claimed: bool, preview_ids: [...], count: int }
       —— 无 avt_anon / session 过期 / 无 eligible record（读侧 miss）→ 200 { claimed:false, count:0 }（no-op）
       —— DB 写失败 → 503 retryable（绝不 200，v2 #5）
```

### 5.2 原子绑定逻辑（伪代码，**非实现代码**；v3.1 终态）
```
require_same_origin_state_change(request)      # v3 #4 CSRF，失败 403
user = current_user                            # 自取，不用 require_auth
if user is None: return 401                     # v2 #2：显式挡 None

avt_anon = request.cookies.get("avt_anon")      # HttpOnly bearer
if not avt_anon: return 200 {claimed:false}     # 读侧 miss

session_id_hash   = hash_scope_key(avt_anon, secret=...)                     # 与 anonymous_session 同款
stored_session_key = hash_scope_key(f"sess:{session_id_hash}", secret=...)   # v2 #1：record 侧派生

# --- 事务开始 ---
# v3.1 #1：record 绑定 = 单条原子 UPDATE...RETURNING（无 SELECT 窗口、无半状态）
bound = UPDATE anonymous_preview_records
          SET claim_user_id = :user_id,                                    # v2 #4：owner 列（migration 040）
              expires_at = GREATEST(expires_at, :now + CLAIM_RETENTION),   # 延长（覆盖 stored_upload §6.1）
              audit = jsonb_set(COALESCE(audit, '{}'::jsonb),              # v3.1 #2：COALESCE 防 NULL
                                '{claimed_at}', to_jsonb(:now))
          WHERE anonymous_preview_records.session_id = :stored_session_key
            AND anonymous_preview_records.expires_at > :now
            AND (claim_user_id IS NULL OR claim_user_id = :user_id)        # 他人占→不命中
          RETURNING preview_id
if bound is empty:
    rollback; return 200 {claimed:false}        # v3.1 #3：无 eligible / 全被他人占/过期 → 统一 no-op（防探测）

# v3 #1+#2：认领 session 锁——限定列 + RETURNING（不用 rowcount）
won = UPDATE anonymous_sessions
        SET claim_user_id = :user_id
        WHERE anonymous_sessions.session_id_hash = :session_id_hash         # v3 #1：非列=列恒真
          AND (claim_user_id IS NULL OR claim_user_id = :user_id)
        RETURNING session_id_hash                                           # v3 #2：RETURNING 决胜
if won is None:
    rollback; return 200 {claimed:false}        # v3.1 #1+#3：session 被他人占（罕见竞态）→ rollback record 绑定，no-op，绝无半状态

commit                                           # 写失败 → rollback + 503（v2 #5）
# --- 事务结束 ---
return 200 {claimed:true, preview_ids: bound, count: len(bound)}
```
> **并发/语义说明**：① record 用单条 `UPDATE...RETURNING` 原子绑定，Postgres 行锁串行化并发同 session 请求，输者条件不命中→空→no-op，无半状态（v3.1 #1）。② 顺序 = 先 record 后 session；session 认领失败回滚 record（绝无 session-claimed-但-record-未绑）。③ 所有 `/claim` 同序加锁（records→session），无锁序反转/死锁。④ **无 409**：他人已认领走统一 `{claimed:false}` no-op（v3.1 #3 防探测）；防误绑他人由条件 `UPDATE`（`claim_user_id IS NULL OR =user`）保证，与返回码无关。⑤ 本人重复 `/claim` → 条件含 `=user` → 幂等 `{claimed:true}`。

### 5.3 安全不变量（CodeX 审查重点）
0. **CSRF / same-origin（v3 #4）**：前置 `require_same_origin_state_change(request)`，失败 403。与既有 `/upload`/`/create` 一致——ambient cookie 状态变更 POST 必须挡跨站。
1. **一次性（v3 #2 用 RETURNING 不用 rowcount）**：`claim_user_id IS NULL` 条件 `UPDATE ... RETURNING`，`.first() is not None` 决胜（**不依赖 asyncpg rowcount**，对齐 [anonymous_preview_api.py:1392](../../gateway/anonymous_preview_api.py:1392) 的对抗审核结论）；重复调用同一用户 → 幂等 200。
2. **先 eligible 后认领（v3 #3）**：先确认存在未过期可认领 record，空则 200 no-op（不写 session）；防"session 被标 claimed 但无可认领产物"。
3. **防重放 / 防绑他人（v3.1 #3 统一 no-op）**：session/record 已被**别的** user 认领 → 条件 `UPDATE` 不命中 → rollback → 统一 **200 `{claimed:false}`**（**不返回 409**，零跨用户信息泄露）。安全保证（绝不改写他人绑定）由条件 `claim_user_id IS NULL OR =user` 维持，与返回码无关。
3. **防越权读探**：不向调用者透露「某 session/preview 是否存在/属谁」之外的信息；按 cookie 自证，不接受 body 指定 preview_id。
4. **server-only 标记不可提权**：认领**不**让客户端把匿名 record/job 提升为非匿名；`is_anonymous_preview` 等 server-only 字段无条件 strip（沿用 create 路径防提权）。
5. **认证强制（CodeX #2）**：**不**只 `Depends(require_auth)`——它在 `auth_required=false` 时返回 `None` 不抛 401（[auth.py:126](../../gateway/auth.py:126)）。端点必须显式 `if user is None: return 401`（绝不在匿名态认领，否则毫无意义且可被滥用刷绑定）。
6. **限频**：`/claim` 加轻量限频（防扫 cookie/撞库式认领），复用既有匿名限频基础设施的 scope-key HMAC 路径。
7. **读 miss vs 写 fail 区分（CodeX #5）**：无 `avt_anon` / session 过期 / 无可认领 record（**读侧 miss**）→ 200 no-op；DB **写**失败 / 事务异常 → **503 retryable + rollback**，绝不 200（避免用户误以为已绑定但实际丢失），绝不部分绑定（事务原子）。

### 5.4 终态结算单一入口（红线）
- 认领**只**改 `anonymous_sessions` + `anonymous_preview_records`，**绝不** UPDATE `jobs.user_id`、**绝不**调 `mirror_job_terminal_state` / `settle_job_credit_ledger` / 任何 capture/release。
- 哨兵 job 继续走 `is_anonymous_preview` 跳分钟结算的既有路径，认领对其**零感知**。
- → 与 `feedback_terminal_state_single_entry` 完全相容（认领不是「把任务推 terminal」的旁路）。

---

## 6. 与现有子系统的交互

### 6.1 TTL sweeper（`gateway/anonymous_preview_sweeper.py`）
- 现状：过期 record（`expires_at < now`）→ 删媒体 + 追加审计 JSONL + 删行；过期 `anonymous_sessions` 行删除。**job 工作区不碰**（只删 gateway 侧上传/teaser + record 行）。⚠️ 被删的"媒体"**包含** `audit.stored_upload_path`（完整原始上传，gateway 上传目录）——这正是转完整要用的源（CodeX #3）。
- 认领后：**延长 record（和 session）的 `expires_at`** 到 `CLAIM_RETENTION`（如 7d，给用户从容转完整），sweeper 凭 `expires_at` 自然跳过 → **完整源 `stored_upload_path` 同步获得延长保护**（无需单独逻辑，因 sweeper 按 record 维度删媒体）。**实现期须验证**：record 延长后，sweeper 对该 record 的 `stored_upload_path` + `teaser_path` 都不删，直到新 `expires_at`。
- **决策点 D4**：`CLAIM_RETENTION` 取值（推荐 7d，admin 旋钮可调）。延长后媒体保留更久 = 存储成本略升，但仅限已认领（=高意向转化用户），可接受；过期后仍由 sweeper 收口。
- session sweeper 删过期行时，已认领 session（`claim_user_id` 非空）是否保留供审计 → 推荐：认领后 session 也延长 `expires_at`，到期照删（绑定真源已在 record/user 侧）。

### 6.2 配额 / 限频
- 认领**不**消耗匿名预览配额（它是 post-preview 动作，不是新 intake）。
- `/claim` 自身加独立轻量限频（§5.3.6）。

### 6.3 付费 clone 红线
- 认领过程零 clone。匿名 express lane 若曾做 CosyVoice **免费临时**克隆，其音色归哨兵所有 + `temporary_expires_at`，由既有 temp-voice cleanup 收口。
- 转完整若要真实音色，走正常 create 的 gated 路径（用户显式 consent + 预扣，CLAUDE.md 白名单豁免）。**认领不迁移、不复用、不重克隆任何音色**（见 §6.4）。

### 6.4 匿名 express 临时音色（不迁移）
- **决策点 D5**：是否把匿名 express 的 CosyVoice 临时音色迁到真实用户库？推荐**不迁移**（增加 sentinel→user 音色所有权迁移复杂度、且临时音色本就短 TTL）。转完整时按正常 gated 流程重克隆/选库音色。

### 6.5 转完整（认领后的下一步，本方案不实现）
- 认领后，前端用保留的 source metadata + 所选方案，引导用户走**正常 create**（新任务、真实用户所有、正常计费/交付）。
- **⚠️ 完整任务的媒体源 = `audit.stored_upload_path`（v2 #3），不是 teaser**。匿名 create 送进 Job API 的 `source.value`/`source_ref` 是裁剪后 teaser（[anonymous_preview_api.py:1433](../../gateway/anonymous_preview_api.py:1433)/[:1523](../../gateway/anonymous_preview_api.py:1523)）；完整原始上传只在 `audit["stored_upload_path"]`（[:528](../../gateway/anonymous_preview_api.py:528)）。转完整端点必须从认领的 record 取 `stored_upload_path` 作 source（server 派生，**不**信客户端传源）。前置=§6.1 保留期延长已保住该文件。
- **路径 + 哈希深度校验（v3 #5，转完整前必过）**：① 文件存在；② **规范化后位于匿名上传根目录内**（`Path.resolve()` + `is_relative_to(upload_root)`，防路径遍历/污染）；③ **不等于 `teaser_path`**（防误用裁剪片）；④ **文件 hash 匹配 `record.source_hash`**（防陈旧/被替换文件）。任一不过 → 4xx 拒，不建任务。
- 这与登录用户的 `preview_reuse_service`（convert）是不同入口（匿名 lane 无 600 预扣 reservation 可结转），故**不复用** convert 的钱逻辑——认领纯元数据桥，转完整=干净的新付费任务。
- **决策点 D7（实现期）**：「转完整」是认领端点顺带返回可用 source 让前端发起普通 create，还是新增专用 `POST /claim-convert` 端点（server 取 `stored_upload_path` 直接建正式任务）？推荐后者（source server 派生、不经客户端、防越权），但属认领之后的独立切片。

---

## 7. 前端改动（设计，不实现）
- 预览 ready：设非敏感提示位（localStorage/非 HttpOnly hint），记「本会话有可认领预览」。
- 登录/注册成功回调：若提示位存在 → 调 `POST /gateway/anonymous-preview/claim`；成功后清提示位 + 用保留元数据引导转完整。
- 登录墙文案沿用 06-01 §17.2（按动作）。
- 新增 `frontend-next/src/lib/api/` 薄封装（fetch，沿项目约定，无 axios/react-query）。

---

## 8. 测试计划（TDD，实现期）
- **认领核心**：一次性（重复调用幂等 `{claimed:true}`）、无 avt_anon→no-op、过期 session→no-op、未登录→401。
- **v3.1 #3 统一 no-op（去 409）**：他人已认领 session/record → **200 `{claimed:false}`**（**非 409**）；断言**未改写**他人绑定（owner 仍是原 user）。
- **v3.1 #1 无半状态**：模拟 session 认领失败（被他人占的竞态）→ record 绑定**整体 rollback**（断言 record `claim_user_id` 仍 NULL，无 session-claimed-但-record-未绑）。
- **v3.1 #2 jsonb NULL**：`audit IS NULL` 的 record 认领后 `audit.claimed_at` 确实写入（非 NULL）。
- **v3 #4 CSRF**：缺/错 Origin → 403（跨站 POST 被挡）。
- **v3 #3 先 eligible**：session 存在但无未过期可认领 record → 200 no-op **且 session 未被标 claimed**（断言 `claim_user_id` 仍 NULL）。
- **v3 #1/#2 SQL/并发**：并发同 session 双请求只一个 winner（RETURNING 决胜，非 rowcount）；认领锁不误绑其它 session 行（限定列条件）。
- **v3 #5 完整源校验**：`stored_upload_path` 不存在/出上传根/等于 teaser/hash 不匹配 → 4xx 拒，不建任务。
- **CodeX #1 session 派生**：record `session_id` 用 `hash_scope_key("sess:"+hash)` 存入，`/claim` 必须用同款派生才查得到（反向断言：用裸 hash 查 → 0 命中）。
- **CodeX #2 auth None**：`auth_required=false` + 无 session → `/claim` 仍 401（不因 require_auth 放行 None）。
- **CodeX #3 完整源**：转完整取 `audit.stored_upload_path`（非 teaser）；teaser 路径不得作完整任务 source。
- **CodeX #5 错误码**：读 miss → 200 no-op；模拟 DB 写失败 → 503（非 200），且 record 未部分写。
- **原子性**：并发两请求同 session（条件 UPDATE 决胜，只一个 winner）。
- **红线守卫**：认领路径 AST 扫**不** import/调用 `mirror_job_terminal_state`/`settle_*`/任何 clone provider；不 UPDATE `jobs.user_id`；body 夹带 `user_id/job_id/is_anonymous_preview` 被 strip。
- **保留字段 + 媒体**：认领后 record 的 source/consent/audit/估价/合规完整可读；`expires_at` 已延长；sweeper 跳过已认领 record **且不删 `stored_upload_path`/`teaser_path`** 直到新 TTL。
- **限频**：`/claim` 限频生效。

## 9. 分阶段实施（实现期，逐阶段 CodeX 外审）
- **T1 数据层**：migration 040 = `anonymous_preview_records.claim_user_id`（UUID NULL + 索引，**必做**，CodeX #4）；`AnonymousSession.claim_user_id` 启用为认领锁；ORM/store 同步 + 保留字段（含 `stored_upload_path`）读路径。
- **T2 认领端点**：`POST /claim`（前置 CSRF same-origin〔v3 #4〕 + 自取 user 显式 None→401〔v2 #2〕 + `hash_scope_key("sess:"+hash)` 反查〔v2 #1〕 + **先查 eligible 后认领**〔v3 #3〕 + `UPDATE...RETURNING` 限定列决胜〔v3 #1/#2〕 + 防越权/重放 + 限频 + 读 miss/写 fail 区分〔v2 #5〕），红线守卫测试。
- **T3 sweeper 交互**：认领延长 `expires_at`（覆盖 `stored_upload_path`/`teaser_path`〔CodeX #3 保住源〕）；sweeper 跳过已认领；`CLAIM_RETENTION` admin 旋钮。
- **T4 前端**：提示位 + post-login 认领 + 转完整引导 + 文案。
- **T5 转完整（可独立切片，D7）**：从认领 record 取 `stored_upload_path` 建正式任务（source server 派生）。
- **T6 部署 gate（项目主）**：默认行为安全（无 flag 时认领端点存在但 no-op 友好）；灰度/冒烟。

## 10. 默认安全 / 灰度
- **admin `anonymous_preview_claim_enabled` 默认 OFF（v3.1 #4）**：虽不碰钱/clone，但它延长媒体保留 + 新增认证写端点 → 默认关，灰度确认后再开（**不**默认 on）。flag 关时 `/claim` 端点存在但直接 200 `{claimed:false}` no-op（或 404，实现期定，推荐 no-op 友好）。
- 全程对既有匿名/登录路径 inert（不改 create/status/stream/settle 任何字节）。

---

## 11. 留给项目主 + CodeX 的决策点汇总
- **D1**：绑定模型 = Model A 元数据桥（推荐）vs Model B 任务升级。
- **D2**：凭证 = `avt_anon` cookie-bearer（推荐）vs 显式下发 token。
- **D3**：触发 = 自动认领 + 可选显式入口（推荐）vs 仅显式按钮。
- **D4**：`CLAIM_RETENTION` 取值（推荐 7d）。
- **D5**：匿名 express 临时音色是否迁移（推荐不迁移）。
- **D6**：`/claim` admin gate `anonymous_preview_claim_enabled`（v3.1 拍板**默认 OFF** 灰度；项目主确认）。
- **D7**（CodeX #3 衍生）：转完整 = 认领端点返回可用 source 让前端发普通 create，vs 新增专用 `POST /claim-convert`（server 取 `stored_upload_path` 直建）。推荐后者（防越权），独立切片 T5。

---

## 12. 一致性自检（against 红线）
- ✅ 终态结算单一入口：认领不改 job user_id、不触 settle/mirror。
- ✅ 付费 clone：认领零 clone；转完整走 gated 显式路径。
- ✅ 匿名标记 server-only：认领 strip 客户端夹带，不提权。
- ✅ 与 convert/reuse 不冲突：不同入口、不复用钱逻辑。
- ✅ 复用既有占位（`claim_user_id` / `claim_token_placeholder`），最小新增、无新付费表。
- ✅ CSRF / same-origin（v3 #4）：与既有 `/upload`/`/create` 同款前置。
- ✅ 并发安全（v3 #1/#2/#3）：限定列 + RETURNING 决胜 + 先 eligible 后认领，无恒真宽更新、无 rowcount 误判、无空认领。
- ✅ 完整源防污染（v3 #5）：路径在上传根内 + 非 teaser + hash 匹配。
- ✅ 无半状态 + 语义一致（v3.1 #1/#3）：record 单条 RETURNING + session 失败回滚 record；他人已认领统一 200 no-op（去 409、防探测）。
- ✅ jsonb NULL 安全（v3.1 #2）：`COALESCE(audit,'{}')`。
- ✅ 灰度保守（v3.1 #4）：`anonymous_preview_claim_enabled` 默认 OFF。
