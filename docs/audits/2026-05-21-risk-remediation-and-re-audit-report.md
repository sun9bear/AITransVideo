# AIVideoTrans 风险修复与代码再审计报告（2026-05-21 修订版）

> **审计时间**：2026-05-21  
> **审计范围**：近期 Critical/P0 修复、安全边界、编辑提交一致性、Alembic 元数据、会话 Cookie、CI 与前端轮询治理  
> **验证依据**：当前工作区源码、`docs/graphs/GITNEXUS_PROJECT_GRAPH.md` 与相关子图、局部回归测试  
> **修订说明**：本版修正了原报告中过度绝对化或与当前仓库不一致的结论，尤其是 CI、SameSite、内部端点防护层级和前端轮询现状。

---

## 摘要

近期 P0 风险的大部分代码修复已经落地：`/source-metadata` 与 `/metering` 已挂内部访问依赖，编辑切分后的音频同步闸门已补强，Alembic 环境已显式导入 sibling 模型，预飞校准也已在 voice selection approve 代理前接入。

但“已完全闭环”这个结论仍需收敛为分层表述：

1. **最小闭环已完成**：多数高危项的生产代码已经按风险方向修正，`internal_expire_voice` 直接端点测试、Alembic import guard、split audio sync CI 覆盖已经补齐。
2. **剩余决策集中在 SameSite/CSRF**：当前 `samesite="lax"` 与旧规划不一致，但旧规划“仅手机验证码、无邮箱流程”的前提已经变化；应作为安全/产品兼容性决策处理。
3. **CI 已存在且已补入本轮关键守卫**：仓库已有 `.github/workflows/ci.yml`；本轮新增 `tests/test_user_voice_internal_access.py`、Alembic sibling model import guard 与 `tests/test_p0_8_split_audio_sync.py` 到 P0 remediation 段。
4. **God Module 治理方向正确，但数据需更新**：当前 `src/pipeline/process.py` 约 10,901 行，`gateway/job_intercept.py` 约 3,756 行，应继续按阶段抽取，不宜大爆炸重写。

---

## 一、审计项及修复状态矩阵

| 风险 ID | 审计关注项 | 当前状态 | 证据 | 残留问题 / 建议 |
| :--- | :--- | :--- | :--- | :--- |
| **S-CRITICAL-1** | `/source-metadata` 与 `/metering` 越权访问 | **源码已修复，测试部分覆盖** | [`gateway/main.py`](../../gateway/main.py) 为两个路由挂载 `Depends(_require_internal_access)`；[`gateway/voice_catalog_api.py`](../../gateway/voice_catalog_api.py) 的 `_require_internal_access` 校验 `X-Internal-Key` 与 loopback；[`gateway/startup_checks.py`](../../gateway/startup_checks.py) 启动时强校验内部 key | 保留现有 CI 中的 pipeline header 测试；建议补充路由级缺 key / 错 key / 非 loopback 行为测试 |
| **S-CRITICAL-2** | `internal_expire_voice` 越权风险 | **已完成最小闭环** | [`gateway/user_voice_api.py`](../../gateway/user_voice_api.py) 的 `/api/internal/user-voices/expire` 入口调用 `_internal_access_error`；helper 现已校验 `X-Internal-Key` 与 loopback；[`tests/test_user_voice_internal_access.py`](../../tests/test_user_voice_internal_access.py) 覆盖缺 key / 非 loopback / key 未配置 / 授权 expire 路径；[`Caddyfile`](../../Caddyfile) 继续阻断公网 `/api/internal/*` | 当前生产 Compose 使用 host network 与 `127.0.0.1:8880`，loopback 校验兼容；若未来改为 Docker service-name 网络调用，需改为可信内网 allowlist |
| **B-CRITICAL-1** | 空闲扫描清理器启动崩溃 | **源码已修复** | [`src/services/web_ui/cleanup.py`](../../src/services/web_ui/cleanup.py) late import 改为 `from services.web_ui import editing_idle_scanner` | 当前方向正确；建议保留结构性 import guard，避免再次引入 `src.` 前缀 |
| **B-CRITICAL-2** | 新切分 Segment 绕过音频同步检验 | **已完成最小闭环** | [`src/services/jobs/editing_commit.py`](../../src/services/jobs/editing_commit.py) 对 baseline 不存在的新 segment 要求 fresh draft WAV；[`tests/test_p0_8_split_audio_sync.py`](../../tests/test_p0_8_split_audio_sync.py) 覆盖 split halves 无 draft / 有 draft 场景，并已加入 CI P0 remediation 段 | 后续只需保持 CI 覆盖 |
| **D-CRITICAL-3** | Alembic autogenerate 误提议 DROP TABLE | **已完成最小闭环** | [`gateway/alembic/env.py`](../../gateway/alembic/env.py) 显式导入 `voice_catalog_models`、`background_task_models`、`label_task_models`，让 `Base.metadata` 完整注册；[`tests/test_gateway_lazy_init_smoke.py`](../../tests/test_gateway_lazy_init_smoke.py) 增加 sibling model import guard，并已加入 CI P0 remediation 段 | 条件允许时可进一步增加真实 Alembic metadata/autogenerate check |
| **N/A** | Preflight 自动校准未接入 approve 路径 | **已接入，降级语义合理** | [`gateway/job_intercept.py`](../../gateway/job_intercept.py) 在 `_approve_voice_selection_with_quality_sync` 中先 `rollback()`，再于 proxy 前调用 `pre_flight_calibrate_voices` | 当前异常处理是非阻塞降级；后续可把 preflight outcomes 暴露给前端或 admin 观测 |
| **N/A** | accept-draft 阶段不落 `tts_input_cn_text` | **策略合理** | [`src/services/jobs/editing_commit.py`](../../src/services/jobs/editing_commit.py) 只在 commit promote draft WAV 后原子 stamping `tts_input_cn_text` | 符合“commit 前不修改 baseline audio”的编辑不变量；建议保留现有测试 |
| **S-DEBT** | Session Cookie SameSite 策略 | **仍为 active decision，不应简单描述为已知修法** | [`gateway/auth.py`](../../gateway/auth.py) 与 [`gateway/support_api.py`](../../gateway/support_api.py) 仍为 `samesite="lax"`；`tests/test_auth_phone.py` 当前断言 Lax；`email_registration_enabled=True` | 需要重新评估邮箱注册、外部跳转、同源部署与移动端兼容后，再选择 Strict 或 Origin/Referer/CSRF token 方案 |
| **CI** | 自动化回归守卫 | **已补强本轮关键守卫** | [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml) 已运行 backend guard、P0 remediation、frontend lint/typecheck；本轮新增 internal expire、Alembic import guard、split audio sync 到 P0 remediation 段 | 后续随 SameSite/CSRF 决策补入对应安全测试 |

---

## 二、关键风险复核

### 1. 内部端点安全边界

`/job-api/jobs/{job_id}/source-metadata` 与 `/job-api/jobs/{job_id}/metering` 的路由层防护已经成立：入口路由挂了 `_require_internal_access`，该依赖要求内部 key 匹配，并拒绝非 loopback 来源。这一项可以认定为源码层修复完成。

`user_voice_api` 下的 `/api/internal/user-voices/*` 已统一为代码层 `X-Internal-Key` + loopback 校验，公网阻断仍由 Caddy 的 `/api/internal/*` block 提供边缘层防护。当前生产 Compose 中 app 与 gateway 都使用 host network，内部调用默认 `127.0.0.1:8880`，所以 loopback gate 与现有部署一致。

残留注意点：如果未来改为 Docker bridge network 并通过 service name 调用 gateway，`request.client.host` 将不再是 loopback，需要把该校验升级为可信内网 / 反代 allowlist。

### 2. 编辑切分后的音频同步

编辑提交前的同步闸门已经修正：当 `editing/segments.json` 中的新 segment 不存在于 baseline `editor/segments.json` 时，必须存在对应 draft WAV 才能通过；否则会进入 `EditingAudioSyncRequiredError`。这正好守住了编辑图谱中的核心约束：split-many 是正式编辑模型，不能让新 ID 绕过重合成和对齐。

这一项已有 `tests/test_p0_8_split_audio_sync.py`，本次局部验证通过，并已加入当前 GitHub Actions 的 P0 remediation 列表。

### 3. Alembic 防误删表

`gateway/alembic/env.py` 已显式导入 sibling 模块，让 `Base.metadata` 能看到 voice catalog、background task、label task 等表。这能降低 autogenerate 误判生产表消失并提议 `drop_table` 的风险。

本轮已补轻量测试断言 env.py 中保留这些导入。后续如果 CI 环境具备数据库能力，可以再增加 Alembic metadata/autogenerate 检查。

### 4. 预飞校准接入

`_approve_voice_selection_with_quality_sync` 已在代理到上游 Job API 之前执行 review preflight calibration，并在长耗时校准前 `rollback()` 释放路由 DB 连接。这一点符合“不要持 route DB 跨 paid/long-running call”的方向。

当前异常处理采用降级继续，不阻断用户审核提交，合理。后续可把 `preflight_outcomes` 写入响应或 admin 观测面，便于发现校准命中率和 timeout。

---

## 三、SameSite / CSRF 决策

### 当前事实

- `gateway/auth.py` 的 session cookie 仍是 `samesite="lax"`。
- `gateway/support_api.py` 的匿名客服 cookie 也是 `samesite="lax"`，但它不是登录 session cookie，风险性质不同。
- `tests/test_auth_phone.py` 当前测试名和断言都把 Lax 当作“mobile compatible”契约。
- 旧规划 `docs/plans/2026-04-17-migration-debt-fixes.md` 认为 Strict 无业务影响，前提是“唯一活跃认证流是手机验证码、无邮件链接跳回场景”。当前 `gateway/config.py` 默认 `email_registration_enabled=True`，此前前提已经不再完整成立。
- 前端 Job API 默认相对路径 `/job-api`，当前部署仍偏同源路径路由；这有利于收紧 Cookie，但仍需实测登录、注册、重置密码、客服、支付回跳和外部营销入口。

### 风险判断

`SameSite=Lax` 已能缓解大多数跨站 POST 自动携带 Cookie 的场景，但不是完整 CSRF 防线。剩余风险主要来自：

- 存在副作用的 GET 端点漏网；
- 同站不同源或子域接管类风险；
- 未来新增支付、客服、邮箱、营销跳转时的跨入口状态变化；
- 没有 Origin/Referer 或 CSRF token 作为第二道应用层校验。

`SameSite=Strict` 可以继续降低跨站携带 session 的风险，但它可能影响外部入口首次导航、邮箱/支付/营销回跳等体验。当前不建议在报告里写成“立即一行改 Strict 即可”，而应先完成产品路径确认。

### 建议路线

1. 短期先审计 state-changing API，确认 GET 无副作用。
2. 对认证态的状态变更请求增加 Origin/Referer 白名单校验，或设计 CSRF token。
3. 若仍决定改 Strict，同步修改 `auth.py`、相关测试、移动端/外部跳转验收用例；`support_api.py` 的匿名 cookie 单独评估，不必机械跟随 session cookie。
4. 把 SameSite 策略写入安全测试，避免未来在 Lax/Strict 之间无记录漂移。

---

## 四、CI 与测试建议

当前 CI 已存在，不能再写“缺失 GitHub Actions”。现有 `.github/workflows/ci.yml` 覆盖：

- `tests/test_gateway_startup_checks.py`
- `tests/test_phase1_guards.py`
- `tests/test_legacy_cleanup_guards.py`
- P0 remediation tests，包括本轮新增的 internal expire、Alembic import guard、split audio sync
- frontend `npm run lint` 与 `npx tsc --noEmit`

剩余建议补入：

1. SameSite/CSRF 策略测试：根据最终决策更新 `tests/test_auth_phone.py`，不要继续让测试名与真实安全策略冲突。
2. 条件允许时增加真实 Alembic metadata/autogenerate check，覆盖轻量字符串 guard 之外的运行时行为。

---

## 五、前端轮询治理

原报告“4 秒无差别强行轮询”的描述过于笼统。当前项目列表页只在存在 active job 时启动 4 秒轮询；背景任务 hook 已有终态停止与错误 backoff。

更合适的治理顺序是：

1. 先复用现有 hook，补 visibility pause、请求去重、in-flight guard 和状态条件。
2. 对任务列表、通知、后台 task 轮询分别设定频率，不做一刀切。
3. 如果轮询逻辑继续扩散，再评估 TanStack Query 或 SWR。当前阶段不建议为了轮询治理引入新框架依赖。

这更符合当前执行阶段“轻量、可测试、可逆”的原则。

---

## 六、长期架构治理

`process.py` 与 `job_intercept.py` 的膨胀仍是维护风险，但治理方式应保持增量迁移：

- `src/pipeline/process.py` 当前约 10,901 行，建议按 ingestion、review、translation、TTS、alignment、delivery/reporting 做阶段性抽取。
- `gateway/job_intercept.py` 当前约 3,756 行，建议优先抽取边界清晰的 lifecycle、quota/settlement、post-edit proxy、download/R2 routing。
- 每次抽取都要保持 `main.py` 与 `pytest` 在 clean local 环境可运行。
- 不要把 Smart、Commercialization、Post-edit 的当前 staged v2 迁移变成大爆炸重写。

---

## 本次复核运行的局部测试

```bash
pytest -q tests/test_p0_8_split_audio_sync.py tests/test_legacy_cleanup_guards.py::test_caddyfile_has_internal_block_rule tests/test_process_pipeline.py::test_report_job_metering_sends_internal_key tests/test_auth_phone.py::TestSessionCookie::test_create_session_uses_mobile_compatible_lax_cookie
```

结果：

```text
12 passed, 1 warning
```

完成最小闭环后，追加验证：

```bash
pytest -q tests/test_gateway_startup_checks.py tests/test_phase1_guards.py tests/test_legacy_cleanup_guards.py tests/test_metering_payload_builder.py tests/test_process_pipeline.py::test_report_job_metering_sends_internal_key tests/test_job_metering_writeback.py::TestReportJobMeteringCallback tests/test_user_voice_internal_access.py tests/test_gateway_lazy_init_smoke.py::test_alembic_env_registers_sibling_model_tables tests/test_p0_8_split_audio_sync.py tests/test_gateway_editing_commit_sync.py::test_consume_post_edit_tts_usage_records_trial_allowance tests/test_gateway_editing_commit_sync.py::test_consume_post_edit_tts_usage_accumulates_existing_usage tests/test_gateway_editing_commit_sync.py::test_consume_post_edit_tts_usage_rejects_segment_limit_without_increment tests/test_gateway_editing_commit_sync.py::test_consume_post_edit_tts_usage_rejects_char_limit_without_increment tests/test_gateway_editing_commit_sync.py::test_consume_post_edit_tts_usage_accepts_paid_plan_limits tests/test_aligner_concurrency.py::test_align_all_parallel_first_error_sets_stop_event_and_skips_paid_fallback tests/test_aligner_concurrency.py::test_align_all_parallel_base_exception_sets_stop_event_and_reraises
pytest -q tests/test_smart_local_integration_smoke.py
```

结果：

```text
97 passed, 1 skipped, 1 warning
6 passed, 1 warning
```

---

## 结论

本轮高危风险修复的主方向是正确的，但原报告把“源码已修复”直接等同为“完全闭环”，结论强度过高。完成本轮最小闭环后，修订判断是：

- **P0 生产代码风险大多已被压住，关键测试和 CI 守卫已补齐**；
- **SameSite/CSRF 仍是单独的 active decision**；
- **架构治理应继续增量推进，不引入重型依赖或大规模重写**。

下一步优先级：

1. 重新决策 SameSite/CSRF：Strict、Origin/Referer、CSRF token 三者至少落一条可测试方案。
2. 对前端轮询做轻量治理：visibility pause、in-flight guard、请求去重和状态条件，不急于引入新框架。
3. 条件成熟后，再做 `process.py` / `job_intercept.py` 的阶段性切片治理。
