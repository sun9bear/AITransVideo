# Voice CPS Auto-Calibration（克隆 + 选择库音色时自动校准）

**触发**: 2026-05-09 production failure on segment_070 — Pre-TTS rewrite 不收敛 + retry path 触发了独立的 strict_retry_reason API bug（已通过 [`78eea96`](#) 修复）。根因之一是 voice CPS estimate 与实际 TTS 速度差太多，rewrite 字数窗口算错，LLM 反复跳出窗口。
**状态**: v4.3 — **T0 开工许可（codex 第六轮）**；T2 开工前需先补 F-v4.3-1（见下文）
**Drafted**: 2026-05-09
**Last updated**: 2026-05-09（v4.3 — codex 第六轮 explicit go for T0 + 3 个 finding cleanup）
**Author**: Claude

**Codex review update (2026-05-09, v1)** — 5 个 finding，全部核实成立，全部已纳入 v2：

- **F1 [P1]**：v1 §1 现状表声称 `POST /gateway/user-voices/{id}/calibrate-speed` 已 rate-limited，事实**不成立**。代码 [`user_voice_api.py:281-355`](../../gateway/user_voice_api.py:281) 的 `calibrate_voice_speed()` 整个函数**没有** `reserve_voice_probe` / `refund_voice_probe` 调用——只有 `probe_user_voice()`（试听）走 P2-23 budget。**v2 处理**：新增 T0 task，独立的 calibration budget（`reserve_voice_calibration` / `refund_voice_calibration`），手动 endpoint + T1 + T2 共用。**不复用** voice probe budget——试听是用户体验额度（10/min, 100/day），校准是系统维护/质量额度（语义不同，配额不同）。
- **F2 [P1]**：v1 T1 checklist 只写 `asyncio.create_task(calibrate_after_clone(voice_id, user_id, provider))`，**没有**说 hook 内部要新开 DB session。`add_user_voice` 已 commit/refresh，但 request 的 `AsyncSession` 在 response 返回后会被 dependency 关闭，background task 复用即 `OperationalError: closed`。**v2 处理**：T1 checklist 显式要求 `calibrate_after_clone` 只接 primitive 参数，内部 `async with async_session() as db:` 重新打开 session 重新 fetch row。测试要 cover "background 不复用 request session"。
- **F3 [P1]**：v1 T2 提议 `asyncio.wait_for(asyncio.to_thread(calibrate_voice, ...), 25)`。但 `voice_speed_calibrator` 内部 [MiniMax synth `timeout_seconds=60.0, max_retries=2`](../../gateway/voice_speed_calibrator.py:124)（最坏 ~180s）+ [ffprobe `subprocess.run` 无 timeout](../../gateway/voice_speed_calibrator.py:155)。即便 `wait_for` 超时返回，**线程内付费调用 + ffprobe 仍在跑**，超时只是"不等了"，没真停。**v2 处理**：T0 引入 calibrator 硬超时改造（per-text synth timeout、ffprobe timeout、可中断的 task 上限），T2 阻塞在 T0 完成之后。
- **F4 [P2]**：v1 §3.3 T3 称 `idx_vc_speed_calibrated` 部分索引可高效查"未校准音色"。但 [migration 012:53](../../gateway/alembic/versions/012_add_voice_speed_calibration.py:47) 实际是 `postgresql_where=chars_per_second IS NOT NULL`——索引覆盖**已校准**行，对 NULL 队列查询**没有帮助**。migration 自己的注释也写反了，本 plan 直接继承了那个错误。**v2 处理**：T3 改成"现 index 不支持 NULL 队列"；如果 catalog 规模大需要新加 `idx_vc_speed_uncalibrated WHERE chars_per_second IS NULL AND archived_at IS NULL`。
- **F5 [P2]**：v1 T2 没说清楚 `_approve_voice_selection_with_quality_sync` 当前是"先 proxy 到 Job API，再 sync Gateway DB"。T2 要"转发前 pre-flight"必须重排顺序：calibrate **必须**在 [`job_intercept.py:1608 proxy_request`](../../gateway/job_intercept.py:1608) 之前完成。**v2 处理**：T2 §3.2 增加"不得在 upstream 已接受后再做校准"硬约束 + payload 提取规则（从 `speakers[]` 读 `voice_id` / `tts_provider` / `minimax_model`）。

**v2 顺序变化**：T0（基础设施）→ T1（克隆 hook）→ T3（admin / 批量）→ T2（review submit）。T2 的同步等待**只在 T0 完成后才合法**；T0 之前禁止做 T2。

---

**Codex review update (2026-05-09, v3 — 整合两轮反馈)**：dedupe 后 12 个 finding（P1×6 / P2×4 / P3×2），全部已纳入 v3：

**v3 加的核心硬约束（贯穿全 plan）**：

> **Calibration 的 key 是全 5 元组 `(scope, owner, provider, voice_id, model_key)`**——不是 voice_id 单字段，也不是 (provider, voice_id) 两字段。理由：
> - 同一 `voice_id` 可能属于不同 user（`user_voices` 不强 unique），见 [`user_voice_api.py:376`](../../gateway/user_voice_api.py:376)
> - 同一 voice 的 MiniMax `speech-2.8-turbo` vs `speech-2.8-hd` CPS 不同；HD 是更精细的模型可能更慢
> - read path 已 model-aware（[`voice_speed_catalog.py:124-153 resolve_chars_per_second`](../../src/services/tts/voice_speed_catalog.py:124) 优先查 `chars_per_second_by_model[tts_model]`，scalar 兜底）
> - **write path 必须对齐**：calibrate 必须显式接 model_key，结果存入 `chars_per_second_by_model[model_key]` JSONB；scalar `chars_per_second` 只作为"最近一次校准的 cross-model 兜底"，**Pre-TTS rewrite 主路径绝不依赖 scalar**

**dedupe 后的 finding 列表**：

- **F1 [P1] in-flight key 串错**（R1+R2 都报）：v2 写 `dict[voice_id, Future]` 会跨用户/跨模型/跨 provider 错误共享结果。**v3 处理**：T0-B 改为 `dict[tuple[scope, owner, provider, voice_id, model_key], Future]`，五元组完全唯一化。
- **F2 [P1] 必须 model-aware**（R2 独有）：MiniMax HD 与 Turbo CPS 不同；现有手动 endpoint 默认 model（[`user_voice_api.py:316`](../../gateway/user_voice_api.py:316) `_DEFAULT_CALIBRATION_MODEL[provider]`），自动化时如果不显式接 model_key，会让 HD 的 voice 读到 Turbo 校准结果，产生 §0 的 CPS 失准问题。**v3 处理**：T0-D 把"calibrate 必须接 model_key、写入 by_model"列为硬约束；T1 克隆默认同时校准 Turbo + HD（用户两个模型都可能选）；T2 按 review payload 解析的 `tts_model` 精确校准；T3 admin 批量也按 model。
- **F3 [P1] CPS 查询要按 model**（R2 独有）：T2 v2 写"查 DB CPS 是否 NULL"——但实际上 scalar 非 NULL 不代表所选 model 已校准。如果 Turbo 已校准、用户选 HD、scalar 用 Turbo 值，T2 会跳过 calibrate → HD pipeline 仍然 CPS 失准。**v3 处理**：T2 lookup 改为 `chars_per_second_by_model[final_tts_model] IS NULL`，scalar 不参与 T2 触发判断。
- **F4 [P1] budget 顺序错**（R1 独有）：v2 伪码先 `reserve` 再进 in-flight。joiner 共享 future 不进付费调用但消耗了配额；refund 又把"未发起" 和"provider 5xx 失败" 混到一个分支。**v3 处理**：T0-A 改为：
  - **in-flight check 先于 reserve**——只有 starter 进入 reserve；joiner 直接 await 现有 future，不消耗额度
  - **refund 仅覆盖 "未发起 paid call" 的失败**：voice 不存在 / unsupported provider / DB 查询前置失败
  - **paid call 已发起后的失败（provider 5xx / synth timeout / 空音频）**：**不 refund**，否则 budget 保护不住失败风暴
  - 新增 `CalibrationResult.paid_call_count: int`（v3-F8 一起做）让上层精确决策
- **F5 [P1] total_timeout 不可兑现**（R1 独有）：v2 写"calibrate_voice() 在 mock synth sleep 70s 时 60s 返回"——但 calibrate_voice 是 sync 函数、`asyncio.wait_for` 不能打断阻塞 thread。**v3 处理**：T0-C 改为：
  - **每个 blocking primitive 自带 timeout**（`_post_json` 单次 ≤ 12s，max_retries=1；`subprocess.run(..., timeout=10)`）
  - **`total_timeout_seconds` 只在每段开始前 + 每个 bounded call 返回后检查预算**，不承诺打断阻塞
  - 测试改成 fake clock + 多个 bounded call 模拟预算耗尽，**不要**写 "sleep 70s 但 60s 返回"
- **F6 [P1] T2 自相矛盾**（R2 独有）：v2 §3.2 风险表写"calibrate 必须在 proxy 之前完成"，但伪码后又写 `do NOT block proxy on result`——实现者按后者会让 pipeline 在 CPS 缺失时启动。**v3 处理**：T2 改为明确"**有上限阻塞等待**（默认 45s），超时后**显式三选一**：成功 → 进 proxy；超时 → log + 进 proxy（fallback 到 default CPS + Post-TTS 反推）；失败但已发起付费 → 同 timeout 分支"。"do NOT block proxy on result" 措辞删除。

- **F7 [P2] T2 函数没有 user 参数**（R1 独有）：v2 伪码用 `user.id`，但 [`_approve_voice_selection_with_quality_sync(request, job_id, db)`](../../gateway/job_intercept.py:1574) 当前签名没 user 参数。**v3 处理**：T2 checklist 加一条"修改签名注入 user，或从 `Job.user_id` 反查 owner_id"——后者更合适，因为 user 已在 require_auth 链上但需要从 db 取出 Job row。
- **F8 [P2] payload 字段命名要统一**（R2 独有）：v2 写 `tts_provider=minimax_tts` + `minimax_model=speech-2.8-hd`，但 review payload 实际是 `provider=minimax` + `minimax_model=hd|turbo`，最后 pipeline 才解析成完整 model id。**v3 处理**：T2 §3.2 加"输入别名 vs 内部规范值" 映射表，明确 preflight 读什么字段。
- **F9 [P2] refund 语义太宽**（R2 独有）：v2 写 `not result.ok` 就 refund——但 paid call 已发起、空音频、provider 5xx 都 not ok 但已花了钱。**v3 处理**：`CalibrationResult` 加 `paid_call_count: int`，refund 仅当 `paid_call_count == 0`（未发起付费调用）。
- **F10 [P2] DB session 寿命**（R2 独有）：v2 T2 复用 request `db` 等待几十秒 calibrate→pool 易打满。**v3 处理**：T2 流程拆成"短 session 查 → 关闭 → background 跑 calibrate（自建短 session 写）→ 再进 approve / proxy"。

- **F11 [P3] §1 现状表残留**（R1 独有）：v2 §1 表里仍说 `idx_vc_speed_calibrated` 用于"哪些音色还没校准"过滤，与后文已经纠正的描述不一致。**v3 处理**：§1 表同步改正。
- **F12 [P3] line number 漂移**（R2 独有）：v2 引用了一些已经过期的行号。**v3 处理**：本轮验证后更新（route 在 1166，function 在 1574，确认无误）。

**v3 顺序保持不变**：T0 → T1 → T3 → T2。但 T0 工作量从 v2 的"3 sub-task"扩到 v3 的"4 sub-task"（新增 T0-D：data model + result schema）；T1 工作量从 "1 model" 扩到 "默认 2 models（Turbo + HD）并发校准"。

---

**Codex review update (2026-05-09, v4 — 整合第三轮审查)**：第三轮 9 个 finding（P1×6 / P2×2 / P3×1），代码核实全部成立，全部已纳入 v4：

- **F-v4-1 [P1] payload 字段名错** — v3 写 `provider`，但前端 [`voiceSelection.ts:27`](../../frontend-next/src/lib/api/voiceSelection.ts:27) 提交、Job API [`review_actions.py:472`](../../src/services/jobs/review_actions.py:472)、Gateway [`job_intercept.py:1560`](../../gateway/job_intercept.py:1560) 全部读 **`tts_provider`**。v3 实现会读不到字段直接跳过 calibrate。**v4 处理**：§3.2 payload 表 + 全部测试名 + 伪码改成 `tts_provider`。canonical 值是小写 `"minimax"` / `"cosyvoice"` / `"volcengine"`（不是 v3 写的 `"minimax_tts"`，那个 v3 已经改过来了，但 v4 再核一遍）。

- **F-v4-2 [P1] T2 必须按"job 最终执行 model"校准，不是 per-speaker `minimax_model`** — 关键代码 [`job_intercept.py:1541-1571 _aggregate_quality_tier_from_speakers`](../../gateway/job_intercept.py:1541)：**任一** MiniMax speaker 选了 `hd` → **整个 job** 的 `tts_model` 变 `speech-2.8-hd`；全部 turbo 才用 `speech-2.8-turbo`。TTS 运行时 [`tts_generator.py:1083-1086 _resolve_minimax_model_for_job`](../../src/services/tts/tts_generator.py:1083) 也是从 job_record 取最终 model。如果 T2 按 speaker 自选的 `minimax_model` 校准（v3 写错了这点），speaker A 选 turbo + speaker B 选 hd → job 跑 hd → A voice 校准了 turbo CPS 但 pipeline 读的是 hd CPS → 仍然失准。**v4 处理**：T2 第一步算 final job-level model；所有 minimax voice 都校准这个 final model；CosyVoice / VolcEngine voice 按 provider-specific 规则派生 model_key（CosyVoice 看 endpoint_mode；VolcEngine 看 resource_id 1.0/2.0）。

- **F-v4-3 [P1] `peek → reserve → get_or_start` 仍有 race** — v3 写 `peek` + 后续 `reserve` + `get_or_start` 是 3 步非原子。两个并发 caller 都可能 peek miss → 都 reserve → 第二个 get_or_start 时变 joiner，但已经消耗了 budget。**v4 处理**：T0-B 改成 **atomic `claim_or_join(key)`**——锁内一次性决定 starter / joiner 并返回 future。只有 starter 才 reserve。joiner 直接 await 同一个 future。

- **F-v4-4 [P1] T1 generic Exception refund 误退已发生的 paid call** — v3 伪码把任意 `Exception` 当 "Pre-paid-call exception" refund，但 `_do_calibrate_in_new_session` 可能是 TTS 已成功、`update_voice_speed_calibration` 写库失败后抛异常，这时已经付费，不应 refund。**v4 处理**：factory **永远返 `CalibrationResult`**（不抛异常）——TTS-success-DB-write-fail 也包成 `CalibrationResult(ok=False, error_class="db_write_failed", paid_call_count=N>0)`。caller **只看 `paid_call_count == 0`** 决定 refund。

- **F-v4-5 [P1] T2 仍可能持有 request `db` 等 50s** — v3 伪码用 route 注入的 `db` 跑 `db.execute(select(Job)...)`，随后 `await preflight_calibrate_voices(...)`。SQLAlchemy 一旦 `execute`，可能持住 connection；preflight 等 50s 期间 connection 卡在 pool 里。**v4 处理**：T2 preflight **完全不用 route `db`**——独立 `async_session()` 查 owner_id 和 CPS，关闭后再跑 calibration。route `db` 只用于原本的 quality_tier sync。

- **F-v4-6 [P1] `wait_for(gather, 50)` + `shield` 语义不安全** — v3 写 `asyncio.wait_for(asyncio.gather(...), timeout=50.0)` + `asyncio.shield` 让已发起的 task 落库。问题：`wait_for` timeout 时会 cancel gather 的 children；shield 只保护单个 awaitable，不能在 gather 嵌套里既"timeout 后立即返回"又"保留 children 跑完"。**v4 处理**：改用 `asyncio.wait(tasks, timeout=50, return_when=ALL_COMPLETED)`——timeout 后**不取消** pending tasks；返回后给 pending tasks 加 `done_callback`，让它们在 background 完成时仍写入 DB（自建短 session）。caller 立即 return 已完成的结果。

- **F-v4-7 [P2] T0-C bounded primitive 只覆盖 MiniMax** — v3 承诺每 voice 60s budget，但 [`cosyvoice_provider.py:24-26`](../../src/services/tts/cosyvoice_provider.py:24) `MAX_RETRIES=5`, `_HELPER_TIMEOUT_SECONDS=90`, backoff base 3 max 60，最坏 90 + (3+6+12+24+48) = 183s 单次。**v4 处理**：T0-C 明确**第一阶段只覆盖 MiniMax**；CosyVoice / VolcEngine 的 calibration bounded primitive 改造拆成独立 sub-task **T0-C-2**（在 T1 落地观察后，T2 / T3 真要扩 provider 时再做）；T1 默认只校准 minimax，T3 admin 批量也先限 minimax。

- **F-v4-8 [P2] 手动 calibrate endpoint 不够 model-aware** — v3 写"手动 endpoint 兜底用默认 model"，意味着用户手动点校准时仍只写默认。**v4 处理**：手动 endpoint [`POST /gateway/user-voices/{id}/calibrate-speed`](../../gateway/user_voice_api.py:281) 改造接 `model_key: str`（query param 或 body），如未传则**默认双 model 并发**（与 T1 行为对齐）。

- **F-v4-9 [P3] T3 编号重复 + "v2 修订" 措辞** — cosmetic 清理。**v4 处理**：移除"v2 修订"标签，编号统一。

**v4 顺序保持不变**：T0 → T1 → T3 → T2。但 T2 工作量从 v3 的 12-16h 升到 16-20h（增加 final-model 派生 + claim_or_join + asyncio.wait + 完整独立 session）。

---

**Codex review update (2026-05-09, v4.1 — 整合第四轮审查)**：第四轮 9 个 finding（P1×5 / P2×3 / P3×1），代码核实全部成立，全部已纳入 v4.1：

- **F-v4.1-1 [P1] 并发写 JSONB 丢 key** — T1 默认双 model 并发跑（turbo + hd）写**同一行** `user_voices`；现有 [`update_voice_speed_calibration` line 173-175](../../gateway/user_voice_service.py:173) 是 `dict(voice.chars_per_second_by_model or {})` 读 → set key → 整体回写——两个 model 并发提交时**后写覆盖前写**，最终只剩一个 model_key。**v4.1 处理**：T0-D 写入 helper 改成 `SELECT ... FOR UPDATE` + merge 或用 PostgreSQL JSONB 原子操作（`jsonb_set`）。新增 regression test：双 model 并发 calibrate → 最终 by_model 含 turbo + hd 两个 key。

- **F-v4.1-2 [P1] route db 在 preflight 前已被使用** — `intercept_job_subresource` 在 dispatch 到 `_approve_voice_selection_with_quality_sync` **之前**已经跑 [`_verify_job_ownership` line 1128](../../gateway/job_intercept.py:1128) → [line 2633 `db.execute(select(Job)...)`](../../gateway/job_intercept.py:2633)。SQLAlchemy session 执行查询后可能持有 connection / transaction。v4 写"完全不用 route db"还不成立——session 在进入 preflight 前已被用过。**v4.1 处理**：T2 进 50s preflight 之前显式 `await db.rollback()` 或 `await db.close()` 释放连接（rollback 通常足够把 connection 还回 pool）。同时新增 regression test：spy `db.execute()` / `db.commit()` 调用——preflight 等待期间 route `db` 必须已 rollback。

- **F-v4.1-3 [P1] T2 `_build_calibration_targets` 返回 `None` 后又判 `None`** — v4 伪码 `_build_calibration_targets` 返回 `(key, None)`（行 825），后面 `_has_cps_in_by_model(target, key.model_key)` 对 `None` 判断（行 904）。这是 v4 自己写的 bug。**v4.1 处理**：把 target 构造与批量查询拆清楚：
  1. 第一阶段：解析 speakers → 列出 `[CalibrationKey, ...]`（不带 row）
  2. 第二阶段：在短 session 内按 `scope` 分组批量查 user_voices / voice_catalog → 拿 `dict[CalibrationKey, by_model_snapshot]`
  3. 第三阶段（关闭 session 后）：按 snapshot 判断 `by_model[model_key] is None` → missing list
  4. 第四阶段：launch tasks for missing list

- **F-v4.1-4 [P1] joiner 必须 shield future** — joiner 当前 `await future`（plan v4 行 534）。如果 HTTP request 断开导致 joiner task 被 cancel，**裸 await 会取消共享 future**，starter 的 `set_result` 会失败，其他 joiner 也受影响。**v4.1 处理**：joiner 改 `await asyncio.shield(future)`；starter 设置结果时也加防御分支：
  ```python
  if not future.done():
      future.set_result(result)
  # else: future was already cancelled or set — log + continue
  ```

- **F-v4.1-5 [P1] 残留旧 method 名 `start_calibration` / `get_or_start`** — v4 T0-A 伪码（行 193）和 T0 checklist（行 1014）仍写 `registry.get_or_start` / `start_calibration`，是 v3 旧名。实现者按 checklist 做会重新引入 reserve race。**v4.1 处理**：全篇 grep & replace 为 `claim_or_join`。

- **F-v4.1-6 [P2] `release(key, future)` 应带 future identity** — 当前 `release(key)` 只按 key 删除 done future。如果第一个 starter 在 set_result 前被异常终止，第二个同 key starter 已注册新 future，第一个的 `release` 会误删第二个。**v4.1 处理**：签名改 `release(key: CalibrationKey, future: asyncio.Future)`，只在 `self._futures.get(key) is future` 时 pop。

- **F-v4.1-7 [P2] `paid_call_count` 必须在 synth 调用前递增** — v4 写 "actually issued" 但没指定时机。如果在 synth 成功后递增，provider 5xx / timeout 抛异常的路径里 `paid_call_count` 仍是 0，会被误判成"未付费 → refund OK"。**v4.1 处理**：`calibrate_voice` 体内**进入 synth 调用之前**先 `paid_call_count += 1`，再调 synth。即使 synth 抛异常 / 超时也保留计数。新增 regression test：mock synth 第一段抛异常 → `paid_call_count == 1`，caller 不 refund。

- **F-v4.1-8 [P2] T2 pending task done_callback 职责不清** — v4 一处说 task / factory 自己开 session 写库（行 843），另一处说 pending 的 callback 完成时写入 by_model（行 864）。两个写入路径会重复或冲突。**v4.1 处理**：明确 **task 自己负责写库**（factory 内部已经 `_do_calibrate_in_new_session` 自建 session）；done_callback **只**记录 log + metrics，绝不碰 DB。

- **F-v4.1-9 [P3] 残留 v3 措辞** — § 5 标题、§ 7 标题、checklist、`asyncio.shield` 旧引用等。**v4.1 处理**：全篇统一去版本标 + 检查 asyncio.shield 引用（v4 已废弃 wait_for+shield 模式，改 asyncio.wait + done_callback；joiner 的 shield 是新的、保留）。

**v4.1 顺序仍不变**：T0 → T1 → T3 → T2。T0 工作量从 v4 的 10-14h 升到 12-16h（+2h：JSONB 原子 update 改造 + paid_call_count 时机校正 + release(key, future) 签名 + future cancel 防御）。

---

**Codex review update (2026-05-09, v4.2 — 整合第五轮审查)**：第五轮 6 个 finding（P1×3 / P2×3），全是局部伪码没同步 v4.1 约束的一致性问题，全部已纳入 v4.2：

- **F-v4.2-1 [P1] T0-D 写入示例字段错** — v4.1 伪码用 `UserVoice.id == voice_id`，但 [`UserVoice.id`](../../gateway/models.py:614) 是 UUID 主键，provider-side voice id 在 [`UserVoice.voice_id`](../../gateway/models.py:620)（String(200)，与 user_id 共同形成唯一约束 `uq_user_voices_user_voice`）。按错字段查询直接返 `None`，写入失败。**v4.2 处理**：T0-D 写入伪码改为 `where(UserVoice.voice_id == voice_id, UserVoice.user_id == user_id)`。同时 voice_catalog 写入需要 `where(VoiceCatalog.provider == provider, VoiceCatalog.voice_id == voice_id)`（catalog 没有 user 维度）。

- **F-v4.2-2 [P1] T1 详细伪码仍是旧 v4** — T0-B 正文已要求 `asyncio.shield(future)` / `release(key, future)` / `if not future.done()`，但 T1 局部伪码（`calibrate_after_clone` 与 `_do_calibrate_in_new_session`）仍是 v4 旧写法：joiner 路径写 `await future`（不带 shield）、`registry.release(key)`（无 future identity）、裸 `future.set_result(result)`（无 done 防御）、`update_voice_speed_calibration(db_bg, voice, cps=...)`（旧签名 + 传 row）。实现者按 T1 小节抄会全套绕过 v4.1 修订。**v4.2 处理**：T1 伪码完全按 v4.1 caller pattern 重写。

- **F-v4.2-3 [P1] T1 factory 设计与 v4.1 helper 矛盾** — v4.1 `update_voice_speed_calibration` 改成"自己 `SELECT ... FOR UPDATE` 重新 fetch row + 行锁"，**不接受** caller 预先 fetch 的 row。但 T1 旧伪码仍 `voice = await fetch_user_voice(db_bg, user_id, voice_id)` 后传 `voice` 进 helper。两套语义在同一 session 上还可能 nested transaction（factory 已 `db_bg` 跑了 select，helper 内再 `db.begin()` 加 FOR UPDATE）。**v4.2 处理**：T1 factory 拆成两步——
  1. **存在性校验**：用独立短 session 仅 `SELECT 1 FROM user_voices WHERE voice_id=:vid AND user_id=:uid`，关闭
  2. **TTS 调用**：`asyncio.to_thread(calibrate_voice, ...)`——返回 `CalibrationResult`
  3. **写入**：调用新签名 `update_voice_speed_calibration(db_w, voice_id=str, user_id=str, cps=..., model_key=...)`，helper 内部自己开 `with_for_update()` 行锁
  - factory 不再持有 row 对象；不在同一 session 内交叉两步 transaction

- **F-v4.2-4 [P2] T2 流程图与正文冲突 done_callback 职责** — 流程图（T2 Phase D）写"done_callback 完成时仍写入 by_model"；正文（v4.1 F-v4.1-8 修订）已纠正为"task 自写 DB / callback 只 log + metrics"。流程图没同步会让实现者双写。**v4.2 处理**：流程图说明改成"pending tasks 加 done_callback 仅 log + metrics（task 自身在 factory 里已写 DB）"。

- **F-v4.2-5 [P2] `CalibrationKey._asdict()` 不存在** — `CalibrationKey` 定义为 `@dataclass(frozen=True, slots=True)`，没有 `_asdict()` 方法（那是 namedtuple）。v4.1 伪码 `dataclasses.asdict(k)` / `key._asdict()` 直接 AttributeError。**v4.2 处理**：全部改为 `dataclasses.asdict(k)`。

- **F-v4.2-6 [P2] catalog 批量查询缺过滤** — v4.1 `_batch_query_by_model_snapshots` 只按 `voice_id.in_(...)` 查 voice_catalog。当前 voice_id 可能跨 provider 唯一但归档音色仍可能命中。T2 第一阶段明确只 MiniMax。**v4.2 处理**：catalog 查询加 `VoiceCatalog.provider == "minimax"` + `VoiceCatalog.archived_at.is_(None)` 双过滤。

**v4.2 工作量**：T0 仍 12-16h（伪码修订不增工时）；T1 4-6h 不变；T3 6-8h 不变；T2 18-22h（伪码同步不增工时）。**总计 40-52h 不变**。

---

**Codex review update (2026-05-09, v4.3 — 第六轮: T0 GO)**：第六轮明确 "**T0 可以按 v4.2 开始实施**"，但提了 3 条 finding：1 条 T2 前置（不阻塞 T0），2 条建议合进文档。全部已纳入 v4.3：

- **F-v4.3-1 [P1] T2 不能只靠 `voice_source == "cloned"` 判 user/catalog**（**T2 开工前必须补，不阻塞 T0**）—— v4.2 `_build_calibration_keys` 用 `voice_source == "cloned"` 走 user scope；但前端 [`VoiceSelectionPanel.tsx:331`](../../frontend-next/src/components/workspace/VoiceSelectionPanel.tsx:331) 用户从"我的音色"复用历史克隆时仍写 `voiceSource: 'catalog'` 提交（[`VoiceSelectionPanel.tsx:475`](../../frontend-next/src/components/workspace/VoiceSelectionPanel.tsx:475)）。结果：复用克隆音色时 T2 误查 `voice_catalog`，漏 `user_voices` 的 model-aware CPS。**v4.3 处理**：作为 **T2 开工前的硬前置 todo** 记进 §5.2 T2 checklist，不动 T0 范围。两种实现路径选一：
  1. **T2 实施时**改 `_build_calibration_keys`：MiniMax voice_id 先 `owner_id + voice_id` 批量查 `user_voices`，命中走 user scope，未命中 fallback catalog
  2. **前端实施时**修复 `VoiceSelectionPanel.tsx` 把"我的音色"复用项的 `voiceSource` 改回 `"cloned"`
  - 推荐方案 1：后端兜底比改前端约束面小，且不依赖前端发布周期。

- **F-v4.3-2 [P2] T0-D 手动 endpoint 也必须不持 route DB 跨 paid call**（**T0 范围内补**）—— v4.2 给 T1/T2 写明独立短 session，但手动 endpoint [`user_voice_api.py:281`](../../gateway/user_voice_api.py:281) 还沿用旧模式：`fetch_user_voice(db, ...)` → `asyncio.to_thread(calibrate_voice)` → `update_voice_speed_calibration(db, voice, ...)`。route db 跨 paid call 持连接 ~30s，且与新 helper 的 `async with db.begin()` 嵌套事务冲突。**v4.3 处理**：T0 实施清单加一条手动 endpoint 改造——
  - 鉴权 + voice 存在性校验后立即 `await db.rollback()` 释放 route session
  - paid TTS 用 `asyncio.to_thread(calibrate_voice, ...)`
  - 写入用新 helper（自建 `SELECT FOR UPDATE` session）
  - 新增测试 `test_manual_calibrate_endpoint_does_not_hold_route_db_across_paid_call`

- **F-v4.3-3 [P3] Option B raw SQL 旧字段残留**（**T0 实施时清掉**）—— `update_user_voice_speed_calibration` 方案 B (`jsonb_set`) 仍写 `WHERE id = :voice_id AND user_id = :user_id`。`UserVoice.id` 是 UUID 主键，provider-side id 在 `UserVoice.voice_id`。**v4.3 处理**：方案 B SQL 改为 `WHERE voice_id = :voice_id AND user_id = :user_id`（与方案 A 字段对齐）。

**v4.3 行动边界**：

| 范围 | 行动 |
|---|---|
| T0 | **现在开工**——按 v4.2 + v4.3 F-v4.3-2 / F-v4.3-3 |
| T1 / T3 | 不动 |
| T2 | **暂停**——开工前先解决 F-v4.3-1（推荐后端 fallback 实现） |

---

## 0. 背景

### Pre-TTS rewrite 的 CPS 依赖链（既有）

详见 [`docs/plans/2026-05-08-p2-17-pipeline-parallelization-plan.md`](2026-05-08-p2-17-pipeline-parallelization-plan.md) 不在此重述；这里只点 Pre-TTS rewrite 的 char-bound 计算：

```
target_chars = target_duration_ms / 1000 × chars_per_second
```

`chars_per_second` 来源（按优先级降序）：
1. **Probe 校准结果** — `voice_catalog.chars_per_second`（库音色）/ `user_voices.chars_per_second`（克隆音色）
2. **Pipeline 反推**（`process.py:7384 _calibrate_tts_duration`）— 跑完 TTS 后从真实 output 反推；只能影响**下一轮**重写
3. **全局 fallback** — 写死 `4.5 chars/s`（[`process.py:7416`](../../src/pipeline/process.py:7416)）

### 当前问题

#### 触发场景

- **场景 A**：用户**首次**用某个 voice（克隆音色或库音色），DB 里 `chars_per_second IS NULL` → fallback 到 4.5
- **场景 B**：英文 speaker 语速快（高 wps），匹配到的音色实际 CPS 低（如 4.0），系统按 4.5 算 target_chars 偏多 → LLM 怎么压都过不了 → 反复 retry → 边界 case 触发 bug

#### 根因之外的设计 limitation

- pipeline 的 `_calibrate_tts_duration` 是**反应式**的——必须先有 TTS output 才能反推。这条不能解决"第一次跑、Pre-TTS rewrite 阶段就要用 CPS"的需求。
- 现有的 `POST /gateway/user-voices/{id}/calibrate-speed` 是**用户手动按按钮**才触发，多数用户不会主动校准。

### 目标

让 Pre-TTS rewrite 在第一次跑就能拿到准确的 voice CPS，不依赖 `_calibrate_tts_duration` 反推、不依赖用户手动校准。

---

## 1. 现状（已有 / 缺失）

### 已有（不要重新发明）

| 组件 | 位置 | 说明 |
|---|---|---|
| Calibration 核心算法 | [`gateway/voice_speed_calibrator.py:179`](../../gateway/voice_speed_calibrator.py:179) `calibrate_voice()` | 3 段标准文本（T1/T2/T3, 458 hanzi 共 ~30s）跑 TTS 测 CPS；返回 `CalibrationResult(ok, cps, per_text, error)`；sanity bounds `[2.0, 8.0] cps`；标准文本由 `gateway/scripts/standard_calibration_texts.py` 导入，与批量脚本共享 |
| 单音色 calibrate endpoint | [`POST /gateway/user-voices/{voice_id}/calibrate-speed`](../../gateway/user_voice_api.py:281) | 鉴权（require_auth），**未** rate-limited（v1 写错，已在 codex review 中纠正）；持久化到 `user_voices.chars_per_second`。T0 必须给它加上 calibration budget |
| 批量 calibrate 工具 | [`gateway/scripts/calibrate_voice_speeds.py`](../../gateway/scripts/calibrate_voice_speeds.py) | 给 `voice_catalog` 全量算 CPS（ops 用） |
| DB schema (用户音色) | migration `013_add_user_voice_speed_calibration` | `user_voices.chars_per_second` (Float) + `chars_per_second_by_model` (JSONB) + `speed_calibrated_at` (DateTime) |
| DB schema (库音色) | migration `012_add_voice_speed_calibration` | `voice_catalog.chars_per_second` (Float) + `chars_per_second_by_model` (JSONB) + `speed_calibrated_at` (DateTime) + 部分索引 `idx_vc_speed_calibrated WHERE chars_per_second IS NOT NULL`。**注意**：该 index 覆盖**已校准**行，不直接支持"未校准列表"查询；migration 自己的注释也写反了，本 plan v1/v2 继承了那个错误。T3 列出未校准用全表扫即可（catalog 规模小），规模大时另加 `idx_vc_speed_uncalibrated`（见 §3.3） |
| CPS read path（已 model-aware） | [`voice_speed_catalog.py:124-153 resolve_chars_per_second`](../../src/services/tts/voice_speed_catalog.py:124) | Priority: 1) `chars_per_second_by_model[tts_model]` 精确匹配；2) `chars_per_second` scalar 兜底；3) None。**v3 关键**：read path 已经是 model-aware；写入端必须对齐 |
| Pipeline 反推校准 | [`process.py:7384 _calibrate_tts_duration`](../../src/pipeline/process.py:7384) | TTS 后从真实 output 反推 CPS（兜底链最末端） |
| 前端手动按钮 | [`voices/page.tsx:44`](../../frontend-next/src/app/(app)/voices/page.tsx:44) `calibrateVoiceSpeed` | 用户手动触发 |
| 克隆 hook 点 | [`voice_selection_api.py:503-514`](../../gateway/voice_selection_api.py:503) `add_user_voice` 调用之后 | T1 的 trigger 落点 |
| voice review submit hook 点 | `review/voice-selection/approve` route 在 [`job_intercept.py:1166`](../../gateway/job_intercept.py:1166) → [`_approve_voice_selection_with_quality_sync`](../../gateway/job_intercept.py:1574)（v3 line 已核：route 1166-1167，function 1574） | T2 的 trigger 落点。当前函数签名 `(request, job_id, db)` **没有** user 参数（v3 codex F7）；T2 需要从 `Job.user_id` 反查 owner |

### 缺失（本 plan 要做的）

❌ **T1**：克隆音色完成后**自动** enqueue calibrate-speed
❌ **T2**：voice review submit 时检查所选音色 CPS，**NULL 则自动**触发 calibrate-speed 后再放行 pipeline
❌ **T3**：admin 后台 + ops 工具：voice_catalog 中未校准音色的可见性 + 一键批量校准

---

## 2. 风险维度通用清单

每个 task 都要按这些维度评估：

1. **付费 API 自动调用合规** — CLAUDE.md 硬约束。Calibrate-speed 一次 ~3 段 × 30s 音频，金钱 ≈ $0.00005，可忽略；但**自动触发**这件事本身需要满足"用户显式动作绑定"。
2. **延迟 / UX** — Calibration 单次 ~10-20s。在 voice clone 后用户已经在等结果（30-60s），叠加无感；在 voice review submit 时用户预期"提交即下一步"，需要明确 progress 反馈。
3. **失败模式** — 网络挂、provider 5xx、音频损坏。失败必须**不阻塞**主流程（克隆成功仍返回；voice review 仍可提交，pipeline 走 4.5 fallback）。
4. **并发 / race** — 同一音色被多个并发请求触发 calibrate-speed → 浪费 TTS 调用。需要去重（per-voice 锁 / 已 in-flight 跳过）。
5. **Rate limit / abuse** — 已有 `reserve_voice_probe`（10/min, 100/day per user）。新增的自动触发是否会撞用户配额？
6. **观测** — 多少音色无 CPS、自动校准成功率、失败原因、平均耗时。没有看板就盲飞。
7. **回滚** — 每个 task 必须有 env feature flag。

---

## 3. 子项详细分析

### 3.0 T0 — Calibration 基础设施前置（T1/T2/T3 全部依赖，v3 扩到 4 个 sub-task）

**目的**：在 T1/T2/T3 之前补完 calibrator 自身的硬约束，否则把 calibration 自动化等于把已有风险放大。

---

**T0-A: 独立的 calibration budget（v3 修订：reserve / refund 顺序与语义）**

新增 `gateway/risk_control.py` 中：

```python
def reserve_voice_calibration(user_id: str) -> Reservation
def refund_voice_calibration(user_id: str, reservation: Reservation) -> None
```

- **不复用** `reserve_voice_probe` 的额度。probe 是用户体验额度（试听给用户听），calibration 是系统维护额度（Pre-TTS rewrite 准确度）。
- 默认配额（待审）：`per-user 5/min, 30/day`。
- 手动 endpoint [`POST /gateway/user-voices/{id}/calibrate-speed`](../../gateway/user_voice_api.py:281) 也接入这个 budget——v1 错认为已 rate-limited，实际没有，独立修。

**关键边界（v4.1 整合）—— reserve / refund 顺序**：

1. **claim_or_join 必须先于 reserve**——只有 starter 进 reserve；joiner 直接 `await asyncio.shield(future)`，**不消耗额度**（v4 codex F-v4-3）。
2. **Refund 仅覆盖"未发起 paid call"的失败路径**：
   - ✅ refund：voice 不存在 / unsupported provider / 输入校验失败 / DB 查询前置失败
   - ❌ **不** refund：provider 5xx、synth timeout、空音频、ffprobe 失败、calibration 后 DB write 失败——任何 paid call 已发起后的失败。理由：refund 这些会让 provider 失败风暴**绕过 budget 保护**，无限消耗。
3. **判定 paid call 是否发起**：通过 `CalibrationResult.paid_call_count: int`（T0-D 引入；**v4.1 codex F-v4.1-7**：synth 调用前递增，不是后），上层 caller 看 `count > 0` 就**不**调 refund。

完整 caller 伪码见下面 §T0-B 的 "Caller pattern（v4.1，原子 claim + joiner shield + future 防御）"。

---

**T0-B: 全 5 元组 in-flight dedupe + atomic claim_or_join（v4 修订：codex v3-F3）**

`gateway/voice_calibration_inflight.py`（新文件）：

```python
import asyncio
from dataclasses import dataclass
from typing import Literal

@dataclass(frozen=True, slots=True)
class CalibrationKey:
    """Full identity of a calibration request. All 5 fields required."""
    scope: Literal["user", "catalog"]
    owner: str           # user_id uuid str for "user"; literal "catalog" for "catalog"
    provider: str        # canonical lowercase: "minimax" | "cosyvoice" | "volcengine"
    voice_id: str
    model_key: str       # canonical model id, e.g. "speech-2.8-turbo"


class CalibrationInFlightRegistry:
    """Atomic claim_or_join (v4 codex F-v4-3 fix).

    v3 design (peek → reserve → get_or_start) had a 3-step non-atomic race:
    two concurrent callers could both peek-miss, both reserve, and only the
    second one find an existing future — the joiner had already wasted a
    budget reservation. v4 fixes this by deciding starter/joiner inside ONE
    lock acquisition.

    starter contract:
      - Caller MUST eventually call set_result(result) on the returned
        future, OR call mark_failed(exc), OR the registry leaks.
      - starter is responsible for budget reservation BEFORE invoking the
        factory; joiner does NOT reserve.
    joiner contract:
      - Just await the returned future. Result is whatever starter set.
      - joiner does NOT reserve budget, MUST NOT refund.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._futures: dict[CalibrationKey, asyncio.Future] = {}

    async def claim_or_join(
        self, key: CalibrationKey
    ) -> tuple[asyncio.Future, Literal["starter", "joiner"]]:
        """Atomically check + claim. starter creates a fresh future and
        returns it; joiner returns the existing future. Both callers
        receive the SAME future for the same key while it's in-flight."""
        async with self._lock:
            existing = self._futures.get(key)
            if existing is not None and not existing.done():
                return existing, "joiner"
            future: asyncio.Future = asyncio.get_event_loop().create_future()
            self._futures[key] = future
            return future, "starter"

    async def release(self, key: CalibrationKey, future: asyncio.Future) -> None:
        """v4.1 codex F-v4.1-6: identity-checked release.

        Only pop if the registry still holds THIS future for THIS key.
        Without identity check, an aborted starter's release() could
        delete a successor starter's freshly-registered future for the
        same key, breaking dedup for new joiners.
        """
        async with self._lock:
            existing = self._futures.get(key)
            if existing is future:
                self._futures.pop(key, None)
```

**Caller pattern（v4.1，原子 claim + joiner shield + future 防御）**：

```python
future, role = await registry.claim_or_join(key)

if role == "joiner":
    # v4.1 codex F-v4.1-4: shield prevents joiner cancellation from
    # propagating into the shared future. If THIS request is cancelled,
    # we stop awaiting but the starter (and other joiners) keep going.
    return await asyncio.shield(future)

# starter path: reserve only AFTER claim succeeds
try:
    reservation = risk_control.reserve_voice_calibration(user_id)
except RateLimitExceeded as exc:
    # v4.1: defensive — future may have been cancelled out from under us.
    if not future.done():
        future.set_exception(exc)
    await registry.release(key, future)
    raise

result: CalibrationResult | None = None
try:
    result = await factory()  # factory ALWAYS returns CalibrationResult, never raises
finally:
    # Refund decision is purely paid_call_count-based (v4 codex F-v4-4)
    if result is not None and not result.ok and result.paid_call_count == 0:
        risk_control.refund_voice_calibration(user_id, reservation)
    elif result is None:
        # Defensive: factory contract is "always returns", but if some
        # internal bug raised, treat as not-yet-paid.
        risk_control.refund_voice_calibration(user_id, reservation)

    # v4.1 F-v4.1-4: defensive set_result — future may have been
    # cancelled before we got here, in which case set_result raises.
    if not future.done():
        future.set_result(result if result is not None else CalibrationResult(
            ok=False, error_class="internal_error",
            paid_call_count=0, model_key=key.model_key, per_text=[], cps=0.0,
        ))
    # else: future was already cancelled or set — log + continue
    await registry.release(key, future)

return result
```

- Joiner 路径完全不进 budget 路径（v4 codex F-v4-3 fix）；shield 隔离 cancel（v4.1 F-v4.1-4）
- Starter 抛 RateLimitExceeded 时 future.set_exception，joiner await 时也会立刻 raise——保证 race 时大家拿到一致的"配额满"结果
- Future 完成（set_result / set_exception）后由 starter identity-checked release（v4.1 F-v4.1-6）
- 全程加 `if not future.done()` 防御 cancel 的 race（v4.1 F-v4.1-4）

---

**T0-C: Bounded primitives + 段间预算检查（v4 修订：第一阶段只 MiniMax，codex F-v4-7）**

**v4 范围澄清**：T0-C 第一阶段**只覆盖 MiniMax**。CosyVoice / VolcEngine 的 calibration bounded primitive 改造拆为独立后续 sub-task **T0-C-2**，在 T1 落地观察后、若 T2 / T3 真要扩 provider 时再做。理由：

- [`cosyvoice_provider.py:24-26`](../../src/services/tts/cosyvoice_provider.py:24) `MAX_RETRIES=5`, `_HELPER_TIMEOUT_SECONDS=90`, `RETRY_BACKOFF_BASE=3.0`, `RETRY_BACKOFF_MAX=60.0`，单次最坏 90 + (3+6+12+24+48) = 183s
- 给 CosyVoice 改 calibration 专用短 timeout 等于动 helper subprocess 协议——风险大于本 plan 触达需求
- T1 默认只校准 MiniMax；T3 admin 批量第一阶段也限 MiniMax；CosyVoice / VolcEngine 库音色靠 ops 批量脚本兜底（参数自定义，无需走 calibrator helper 改造）

**MiniMax bounded primitives（v4 实施目标）**：

[`gateway/voice_speed_calibrator.py`](../../gateway/voice_speed_calibrator.py) 当前两处问题：

- [line 124](../../gateway/voice_speed_calibrator.py:124) MiniMax synth `_post_json(..., timeout_seconds=60.0, max_retries=2)` → 单段最坏 ~180s
- [line 161-169](../../gateway/voice_speed_calibrator.py:161) `subprocess.run([... ffprobe ...])` 无 `timeout` 参数 → 理论上无限等

**改造（每个 blocking primitive 自带 timeout，total budget 只在段间检查）**：

1. **`_post_json` 单次 timeout** 降到 `12.0s`（替换 60s）+ `max_retries=1`（替换 2）。理由：MiniMax 试听 endpoint 实测 p95 < 10s；12s + 1 次重试单段最坏 ≈ 24s。**这就是单段实际可被打断的最长阻塞时间**——不依赖 `total_timeout_seconds` 来打断它。
2. **`subprocess.run(...)` 加 `timeout=10`**（ffprobe 亚秒返回，10s 是肥余度）；超时抛 `subprocess.TimeoutExpired` 由 caller 捕获转 `RuntimeError("ffprobe timeout")`。
3. **`calibrate_voice()` 加可选 `total_timeout_seconds=60.0` 参数**——但语义**不是**"在 sleep 70s 时强制 60s 返回"。语义是：
   - 在每段开始**之前**检查 `elapsed < total_timeout_seconds`，超了就直接返 `CalibrationResult(ok=False, error="total_timeout", paid_call_count=<so_far>)`
   - 在每段 bounded call 返回**之后**再检查一次（避免 段 1 + 段 2 都超还继续跑段 3）
   - 上述两个 check 在阻塞 primitive 边界——**不承诺**打断已经在 `_post_json` 内 12s 等待中的调用
4. **不再说 "可以兑现 25s timeout"**。实际单段最坏 ~24s，3 段最坏 ~72s——total_budget 60s 在中段会触发，让段 3 不再发起新付费调用，但段 1/2 已发出的不能撤回。

**测试**（codex F5）：
- 用 fake clock + 多个 bounded call 模拟"前两段共消耗 55s，段 3 入口检查 elapsed > 60s 直接返"
- **不要**写"sleep 70s 但 60s 返回"——那是不可兑现的承诺
- 测试 `test_total_timeout_skips_remaining_texts_after_budget_exhausted` 验证段 3 没有发起 paid call，`paid_call_count == 2`

---

**T0-D: Model-aware data model + CalibrationResult 扩展（v3 新增 + v4 扩展手动 endpoint，codex F2 + F9 + F-v4-8）**

**核心硬约束**：所有 calibrate 调用必须接 `model_key: str`；写入只填 `chars_per_second_by_model[model_key]` JSONB；scalar `chars_per_second` 留作 cross-model 兜底（read path 已 model-aware）。

**`calibrate_voice()` 签名修订**：

```python
def calibrate_voice(
    *,
    provider: str,           # canonical: "minimax" | "cosyvoice" | "volcengine"
    model: str,              # canonical model id, e.g. "speech-2.8-turbo"
    voice_id: str,
    total_timeout_seconds: float = 60.0,  # T0-C
    synth_fn: Callable | None = None,     # injectable for tests
) -> CalibrationResult:
    """ALWAYS returns CalibrationResult — never raises.

    v4 codex F-v4-4: callers (T1 background, T2 preflight) decide refund
    purely from result.paid_call_count, so any internal exception (including
    DB write failure post-paid-call) MUST be caught and packed into
    CalibrationResult(ok=False, error_class=..., paid_call_count=N).

    v4.1 codex F-v4.1-7 — paid_call_count 计数时机：每次进入 synth 调用
    BEFORE the call. Even if synth raises (provider 5xx, timeout), the
    count must reflect "we issued this call":

        for text in STANDARD_TEXTS:
            paid_call_count += 1   # incremented BEFORE synth attempt
            try:
                wav = synth_fn(text, voice_id, model)
            except Exception as exc:
                # paid_call_count already reflects the attempt
                return CalibrationResult(ok=False, error_class="synth_failed",
                                         paid_call_count=paid_call_count, ...)

    The wrong order ("count after success") would let provider 5xx /
    timeout failures slip through as paid_call_count == 0 and trigger
    spurious refunds, breaking the v4 budget contract.
    """
```

**手动 endpoint v4 修订（codex F-v4-8）**：

[`POST /gateway/user-voices/{voice_id}/calibrate-speed`](../../gateway/user_voice_api.py:281) 改造：

```python
class CalibrateSpeedRequest(BaseModel):
    """Optional model_key override. None means "calibrate ALL canonical
    models for this provider" (mirrors T1 clone behaviour)."""
    model_key: str | None = None

@router.post("/user-voices/{voice_id}/calibrate-speed")
async def calibrate_voice_speed(
    voice_id: str,
    body: CalibrateSpeedRequest | None = None,  # NEW v4
    user: User | None = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> Response:
    # ... existing voice fetch / provider validation ...

    if body is None or body.model_key is None:
        # v4 default: same as T1 — calibrate all canonical models for the provider
        models_to_run = _CANONICAL_MODELS_BY_PROVIDER[provider]  # ["speech-2.8-turbo", "speech-2.8-hd"] for minimax
    else:
        models_to_run = [body.model_key]  # admin/UI specified specific model

    # Run each model's calibration via T0-A budget + T0-B claim_or_join
    # (parallel via asyncio.gather, all going through the same registry)
    ...
```

前端 [`voices/page.tsx`](../../frontend-next/src/app/(app)/voices/page.tsx) 也升级：手动校准按钮默认走"全 model"，admin debug 模式可选指定 model_key。

**`CalibrationResult` 字段补充**（codex F9）：

```python
@dataclass(slots=True)
class CalibrationResult:
    ok: bool
    cps: float                       # 0.0 if not ok
    per_text: list[TextResult]
    error: str = ""                  # human-readable
    error_class: str = ""            # NEW: machine-parseable error category
                                     # "rate_limited" | "voice_not_found" | "unsupported_provider"
                                     # | "synth_failed" | "ffprobe_failed" | "ffprobe_timeout"
                                     # | "total_timeout" | "out_of_bounds_cps"
    paid_call_count: int = 0         # NEW: how many provider TTS calls were
                                     # actually issued (regardless of success).
                                     # Used by caller to decide refund.
    model_key: str = ""              # NEW: which model this result is FOR.
                                     # Mandatory non-empty.
```

**写入 helper（v4.1 修订：JSONB 原子合并，codex F-v4.1-1）**

⚠️ 当前 [`user_voice_service.py:172-175`](../../gateway/user_voice_service.py:172) 的实现：

```python
existing = dict(voice.chars_per_second_by_model or {})
existing[model_key] = float(cps)
voice.chars_per_second_by_model = existing
await db.commit()
```

**这是 read-modify-write 整个 JSONB 字段**。T1 默认双 model 并发跑（turbo + hd 同时校准同一行）：

```
T1 turbo task:                         T1 hd task:
  read by_model = {}                     read by_model = {}
  by_model['turbo'] = 4.5                by_model['hd'] = 4.2
  commit by_model = {'turbo': 4.5}       commit by_model = {'hd': 4.2}  ← 覆盖 turbo
```

**v4.1 必须改成原子合并**。两种合法方案选一（决策点 §7 第 13 条）：

**方案 A — `SELECT ... FOR UPDATE` + 行锁（推荐，v4.2 codex F-v4.2-1 修正字段名）**：

```python
async def update_user_voice_speed_calibration(
    db: AsyncSession,
    *,
    voice_id: str,           # provider-side voice id (UserVoice.voice_id, NOT primary key)
    user_id: str,            # owner uuid string (UserVoice.user_id)
    cps: float,
    model_key: str,
) -> UserVoice:
    """v4.1 codex F-v4.1-1: row lock + read-modify-write in one txn.
    Two concurrent tasks (e.g. turbo + hd) serialize on FOR UPDATE;
    the second sees the first's commit and merges its key on top.

    v4.2 codex F-v4.2-1: query MUST use UserVoice.voice_id (provider-side
    string) NOT UserVoice.id (UUID primary key). Together with user_id
    they form the uniqueness constraint uq_user_voices_user_voice.
    """
    async with db.begin():
        voice = (await db.execute(
            select(UserVoice)
              .where(
                  UserVoice.voice_id == voice_id,    # F-v4.2-1: not UserVoice.id
                  UserVoice.user_id == user_id,
              )
              .with_for_update()
        )).scalar_one_or_none()
        if voice is None:
            raise VoiceNotFoundError(voice_id)
        merged = dict(voice.chars_per_second_by_model or {})
        merged[model_key] = float(cps)
        voice.chars_per_second_by_model = merged
        voice.chars_per_second = sum(merged.values()) / len(merged)  # cross-model mean for tooltip
        voice.speed_calibrated_at = datetime.now(timezone.utc)
        # commit happens at end of begin() block
    return voice


async def update_catalog_voice_speed_calibration(
    db: AsyncSession,
    *,
    voice_id: str,
    provider: str,           # canonical lowercase: "minimax" / etc.
    cps: float,
    model_key: str,
) -> VoiceCatalog:
    """v4.2 catalog variant. voice_catalog is keyed by (provider, voice_id);
    no user dimension. archived rows excluded so admin batch and ops won't
    accidentally write to retired voices."""
    async with db.begin():
        voice = (await db.execute(
            select(VoiceCatalog)
              .where(
                  VoiceCatalog.provider == provider,
                  VoiceCatalog.voice_id == voice_id,
                  VoiceCatalog.archived_at.is_(None),
              )
              .with_for_update()
        )).scalar_one_or_none()
        if voice is None:
            raise VoiceNotFoundError(voice_id)
        merged = dict(voice.chars_per_second_by_model or {})
        merged[model_key] = float(cps)
        voice.chars_per_second_by_model = merged
        voice.chars_per_second = sum(merged.values()) / len(merged)
        voice.speed_calibrated_at = datetime.now(timezone.utc)
    return voice
```

- 优点：纯 ORM，可移植；事务内行锁保证两 task 串行
- 缺点：需要每次 SELECT 一次（小开销）；caller 不能预先 fetch row 后传进来（API 变了）

**方案 B — PostgreSQL 原子 `jsonb_set`**（v4.3 codex F-v4.3-3 修正字段名）：

```python
async def update_voice_speed_calibration(
    db, *, voice_id, user_id, cps, model_key,
):
    # v4.1 codex F-v4.1-1: server-side atomic JSONB update.
    # Two concurrent calls with different model_key are atomic at the
    # DB layer; PostgreSQL serializes the row update internally.
    # v4.3 codex F-v4.3-3: WHERE clause uses voice_id (provider-side string)
    # NOT id (UUID primary key) — same fix as Option A.
    await db.execute(
        text("""
        UPDATE user_voices
           SET chars_per_second_by_model =
               COALESCE(chars_per_second_by_model, '{}'::jsonb)
               || jsonb_build_object(:model_key, :cps),
               speed_calibrated_at = now(),
               updated_at = now()
         WHERE voice_id = :voice_id AND user_id = :user_id
        """),
        {"voice_id": voice_id, "user_id": user_id,
         "model_key": model_key, "cps": cps},
    )
    # cross-model mean update is a separate UPDATE in v4.1 (read JSONB → mean → write scalar);
    # alternatively skip scalar update and let it be lazily computed by a
    # nightly job. **决策点 §7 第 13 条**：是否推迟 scalar 更新到 nightly job
    await db.commit()
```

- 优点：纯 DB 原子，并发完全无 race
- 缺点：raw SQL，不走 ORM；scalar mean 计算需要额外 round-trip

**v4.1 推荐方案 A**——SQLAlchemy 行锁简单直接，可读性好，cross-model mean 一次完成。仅当并发量极高（admin 批量 + 多用户同时跑）需要避免行锁竞争时才考虑方案 B。

**新增 regression test**（critical）：
- `test_concurrent_calibrate_turbo_and_hd_preserves_both_keys`: 并发 2 task 同 voice 不同 model → 最终 by_model 包含 turbo + hd 两个 key（**v4.1 codex F-v4.1-1 regression guard**）

**Pre-TTS rewrite 读取约束**：[`process.py:5237-5240 speaker_chars_per_second`](../../src/pipeline/process.py:5237) 这条链上，最终走 `voice_speed_catalog.resolve_chars_per_second(voice_id, provider=..., tts_model=...)`，必须传 `tts_model`。如果发现 pipeline 某条 path **没**传 model 就读 CPS，那是 read path 的 bug，应在 v3 实施 T1 之前先单独修。本 plan 不直接动 pipeline，但 T0 测试要 cover "pipeline 调 resolve_chars_per_second 必须有 tts_model"。

---

**T0 风险评估（v3 整合）**:

| 维度 | 评估 |
|---|---|
| 付费 API 合规 | T0 不引入新付费调用；只是给已有路径加护栏 + 改 result 数据结构 |
| 失败模式 | 手动 endpoint 加 budget 后，超额返 429 + UI 显示"今日校准额度已用完"；total timeout 返 502 + `error_class="total_timeout"`；refund 严格按 `paid_call_count == 0` |
| 并发 | T0-B 5 元组 key 是核心防护；HD/Turbo 不串、跨用户不串、catalog/user 不串 |
| 观测 | log `[calibration-budget]` `[calibration-inflight]` `[calibration-timeout]`；admin 看板加"24h calibration 数 / inflight 命中数 / timeout 数 / 按 model_key 分布" |
| 回滚 | T0-A env `AVT_CALIBRATION_BUDGET_ENABLED`（默认 true）；T0-B / T0-C / T0-D 永久生效（关闭等于回归 bug，不应有 kill switch） |

**估算**: 8-12 小时（v2 估 4-6h，v3 因 5 元组 key + result 扩展 + write path 改造扩到 8-12h）
**ROI**: T1 / T2 / T3 的前置依赖
**风险**: 低-中（T0-D 改 write path 影响所有 calibrate caller，要小心向后兼容；scalar 字段保留便于读旧数据）

---

### 3.1 T1 — 克隆音色后自动 calibrate-speed（T0 完成后做）

**位置**: [`gateway/voice_selection_api.py:501-516`](../../gateway/voice_selection_api.py:501)

**当前代码骨架**:
```python
if user_id:
    try:
        from user_voice_service import add_user_voice
        await add_user_voice(
            db,
            user_id=user_id,
            voice_id=clone_result,
            label=f"{display_speaker_name} Clone",
            ...
        )
    except Exception:
        logger.exception("Failed to save cloned voice to user library")

return _json_response(200, {"voice_id": clone_result, ...})
```

**改造（v3 修订）**：在 `add_user_voice` 成功之后，enqueue **2 个并发 background calibrate task**——一个跑 `speech-2.8-turbo`、一个跑 `speech-2.8-hd`，分别写入 `chars_per_second_by_model` 的对应键。理由：用户后续在 review 阶段可能选 turbo 也可能选 hd，提前都校准好，T2 多数走 fast pass。不阻塞 clone 的 200 response。Background task **必须**自建 DB session（v2 codex F2）。

**MiniMax 模型策略（v3 关键）**：克隆出来的音色默认两个模型都校准——理由是用户在 review 阶段切档（turbo ↔ hd）的成本应当为 0，不让"切档触发额外等待"。每个模型独立 budget reservation + 独立 in-flight key。如果用户从来不选某个模型，多花一次 ~$0.00005 也可忽略。

**风险评估（v3 修订）**:

| 维度 | 评估 |
|---|---|
| 付费 API 合规 | ✅ Clone 是用户显式动作；calibrate 与 clone 紧绑定。**v3**：现在 clone 后会发起 2 次 paid calibrate（turbo + hd），单价 ~$0.00005 × 2 ≈ $0.0001，仍可忽略 |
| UX 延迟 | ✅ Clone 后台跑 calibrate，UI 不阻塞；2 个 model 并发跑，总时延 ≈ 单 model 时延 ~30s |
| 失败模式 | 任一 model 失败：log + 该 model_key 留缺失；其他 model 不受影响。clone 已成功不阻塞。 |
| DB session（codex v1 F2） | Background task 自建 session：`async with async_session() as db_bg:`。只传 primitive `(voice_id, user_id, provider, model_key)`，不传 row 或 request session |
| 并发 | T0-B 5 元组 in-flight registry 去重。同 voice 的 turbo 和 hd 是**不同 key**，所以并发跑；用户重复点 clone 触发同 (voice, model) 时由 registry 拦掉 |
| Rate limit | 单次 clone 消耗 2 个 calibration budget（每个 model 一个）。配额 5/min, 30/day per user 够用 |
| 观测 | log `[auto-calibrate-clone] voice=X model=Y cps=Z duration_ms=W status=ok/fail/joined`；admin 看板按 model_key 分布 |
| 回滚 | env `AVT_AUTO_CALIBRATE_AFTER_CLONE=true/false` |

**实施要点（v3 修订）**:

1. `add_user_voice` 之后并发 enqueue 两个 task：
   ```python
   if env_var_enabled("AVT_AUTO_CALIBRATE_AFTER_CLONE", default=True):
       for model_key in ("speech-2.8-turbo", "speech-2.8-hd"):
           asyncio.create_task(
               calibrate_after_clone(
                   voice_id=clone_result,
                   user_id=user_id,
                   provider="minimax",   # canonical (v3 codex F8: "minimax" not "minimax_tts")
                   model_key=model_key,
               )
           )
   ```
   **只传 primitive**——不传 db 或 row。

2. `calibrate_after_clone(voice_id, user_id, provider, model_key)` 体（**v4.2 修订：完全对齐 T0-B caller pattern**）：
   ```python
   async def calibrate_after_clone(
       *, voice_id: str, user_id: str, provider: str, model_key: str
   ) -> None:
       """v4 codex F-v4-3 + F-v4-4 + v4.1 codex F-v4.1-4 + F-v4.1-6 fix:
       atomic claim_or_join, joiner shield, identity-checked release,
       factory returns CalibrationResult instead of raising, refund purely
       on paid_call_count == 0."""

       key = CalibrationKey(
           scope="user", owner=user_id, provider=provider,
           voice_id=voice_id, model_key=model_key,
       )

       # T0-B atomic claim_or_join
       future, role = await registry.claim_or_join(key)

       if role == "joiner":
           # v4.1 F-v4.1-4: shield prevents joiner cancellation from
           # propagating into the shared future. If THIS task is cancelled,
           # we stop awaiting but starter (and other joiners) keep going.
           try:
               await asyncio.shield(future)
           except Exception:
               logger.exception("[auto-calibrate-clone] joiner observed failure key=%s", key)
           return

       # starter: reserve only AFTER claim succeeds (v4 F-v4-3)
       try:
           reservation = risk_control.reserve_voice_calibration(user_id)
       except RateLimitExceeded as exc:
           # v4.1 F-v4.1-4: defensive — future may have been cancelled out from under us.
           if not future.done():
               future.set_exception(exc)
           await registry.release(key, future)  # v4.1 F-v4.1-6: identity-checked
           logger.warning(
               "[auto-calibrate-clone] budget exhausted user=%s voice=%s model=%s",
               user_id, voice_id, model_key,
           )
           return

       result: CalibrationResult | None = None
       try:
           result = await _do_calibrate_in_new_session(
               voice_id=voice_id,
               user_id=user_id,
               provider=provider,
               model_key=model_key,
           )
       finally:
           # v4: factory ALWAYS returns CalibrationResult (no exceptions).
           # Refund decision is purely paid_call_count-based.
           if result is None:
               # Defensive: shouldn't happen per factory contract.
               risk_control.refund_voice_calibration(user_id, reservation)
               if not future.done():
                   future.set_result(CalibrationResult(
                       ok=False, error_class="internal_error",
                       paid_call_count=0, model_key=model_key, per_text=[], cps=0.0,
                   ))
           else:
               if not result.ok and result.paid_call_count == 0:
                   risk_control.refund_voice_calibration(user_id, reservation)
               # v4.1 F-v4.1-4: defensive set_result — future may have been
               # cancelled before we got here, in which case set_result raises.
               if not future.done():
                   future.set_result(result)
           await registry.release(key, future)  # v4.1 F-v4.1-6: identity-checked


   async def _do_calibrate_in_new_session(
       *, voice_id: str, user_id: str, provider: str, model_key: str
   ) -> CalibrationResult:
       """v4.2 codex F-v4.2-3: factory does NOT pre-fetch row.
       Two-step structure:
         (a) existence check — independent short session, just SELECT 1
         (b) TTS in thread — calibrate_voice (T0-C bounded, never raises)
         (c) DB write — call new helper update_user_voice_speed_calibration
             which opens its own SELECT FOR UPDATE session

       This avoids nested transactions and matches the v4.1 helper signature
       which doesn't accept caller-fetched rows.

       v4 codex F-v4-4: NEVER raises. All failure modes → CalibrationResult.
       """
       normalized_provider = _normalize_tts_provider(provider)
       if normalized_provider is None:
           return CalibrationResult(
               ok=False, error_class="unsupported_provider",
               paid_call_count=0, model_key=model_key, per_text=[], cps=0.0,
           )

       # Step (a): existence check — independent short session, no row capture
       async with async_session() as db_check:
           exists = (await db_check.execute(
               select(UserVoice.id)  # just need to know it exists
                 .where(
                     UserVoice.voice_id == voice_id,    # v4.2 F-v4.2-1: provider-side string
                     UserVoice.user_id == user_id,
                 )
           )).scalar_one_or_none()
       if exists is None:
           return CalibrationResult(
               ok=False, error_class="voice_not_found",
               paid_call_count=0, model_key=model_key, per_text=[], cps=0.0,
           )

       # Step (b): TTS in thread (T0-C bounded; calibrate_voice never raises)
       result = await asyncio.to_thread(
           calibrate_voice,
           provider=normalized_provider,
           model=model_key,
           voice_id=voice_id,
           total_timeout_seconds=60.0,
       )

       if not result.ok:
           # paid_call_count already accurate; just propagate
           return result

       # Step (c): DB write via new v4.1 helper (it opens its own SELECT
       # FOR UPDATE session, merges JSONB atomically, commits)
       try:
           async with async_session() as db_write:
               await update_user_voice_speed_calibration(
                   db_write,
                   voice_id=voice_id,
                   user_id=user_id,
                   cps=result.cps,
                   model_key=model_key,
               )
       except Exception as exc:
           # v4 F-v4-4: TTS already paid for; DB write failed. Preserve count.
           logger.exception(
               "[auto-calibrate-clone] DB write failed after paid TTS — preserving paid count",
           )
           return CalibrationResult(
               ok=False,
               error_class="db_write_failed",
               paid_call_count=result.paid_call_count,  # preserve!
               model_key=model_key,
               per_text=result.per_text,
               cps=result.cps,
               error=f"db_write_failed: {exc!r}"[:300],
           )

       return result
   ```

3. 全部失败路径只 log，不向上抛；clone 200 已经返回不受影响
4. env `AVT_AUTO_CALIBRATE_AFTER_CLONE`（默认 `true`）+ docker-compose.yml 显式列出
5. log 前缀 `[auto-calibrate-clone]`

**测试策略（v3 修订）**:

- **unit**:
  - `test_clone_enqueues_two_calibrate_tasks_for_turbo_and_hd`: mock calibrate_voice → 验证 add_user_voice 之后被调 2 次（turbo + hd），model_key 分别正确
  - `test_clone_succeeds_when_calibrate_fails`: calibrate 抛异常 → clone 仍 200
  - `test_clone_skips_calibrate_when_env_disabled`: env=false → 0 次 calibrate
  - `test_clone_calibrate_skips_on_budget_exhausted`: T0-A budget 满，calibrate 跳过 + log
  - `test_clone_calibrate_inflight_joiner_does_not_reserve_budget`: 模拟同 (voice, model) 第二个并发 caller，**不**消耗 budget（codex F4 regression guard）
  - `test_clone_calibrate_no_refund_after_paid_call_failed`: provider 5xx 后 paid_call_count=N>0，**不** refund（codex F4 regression guard）
  - `test_clone_calibrate_uses_new_db_session_not_request_session`: spy on `async_session()` 调用次数 + 断言 background task 没碰 request session（codex v1 F2 regression guard）
  - `test_clone_calibrate_writes_to_chars_per_second_by_model_not_scalar`: 验证写入 `by_model[model_key]`，scalar 字段如配置允许可以更新为 mean，但不能只写 scalar（v3 codex F2 + write path 守卫）
- **集成 smoke**（部署后手工）:
  - 真克隆一个 MiniMax voice, 等 60s, `curl /gateway/user-voices/{id}` 看 `chars_per_second_by_model` 含 turbo + hd 两个 key
  - 重复点 2 次"克隆"按钮：每个 model 只 1 次 calibration（in-flight registry 去重）

**估算**: 4-6 小时（v2 估 3-4h；v3 因为双 model + budget 顺序约束 + paid_call_count 增加 1-2h）

**ROI**: **高**——新克隆的音色立刻有准 CPS（双 model），本次生产 video 同类问题不会再触发；HD/Turbo 切档零延迟
**风险**: 低

---

### 3.2 T2 — voice review submit 时对无 CPS 的选定音色 pre-flight calibrate（T0 + T3 完成后做）

**位置**: [`job_intercept.py:1166`](../../gateway/job_intercept.py:1166) route → [`_approve_voice_selection_with_quality_sync()` line 1574](../../gateway/job_intercept.py:1574)（v3 line 已核对）

**当前形态（v3 codex F1+F6 已纠正自相矛盾）**:

```
POST /review/voice-selection/approve
  ↓
_approve_voice_selection_with_quality_sync(request, job_id, db)   [无 user 参数 — codex F7]
  ↓
读 body / 解析 speakers
  ↓
proxy_request → Job API（job_intercept.py:1608）        ← 当前是这里
  ↓
sync Gateway DB（quality_tier / tts_model / metering_snapshot）
  ↓
return response
```

**T2 改造（硬约束）**：calibrate **必须**在 [`proxy_request:1608`](../../gateway/job_intercept.py:1608) **之前**完成。proxy 之后 Job API 已读 review_state.json 启动 pipeline，CPS 写晚了。

**新顺序（v4 修订：final job-level model + 全程独立 session + asyncio.wait 不取消 stragglers）**:

```
POST /review/voice-selection/approve
  ↓
读 body / 解析 speakers[]（字段名 tts_provider, minimax_model 别名 hd/turbo — v4 codex F-v4-1）
  ↓
[v4-F-v4-2 关键] 调用 _aggregate_quality_tier_from_speakers(speakers)
        → 得到 final_minimax_model: "speech-2.8-hd" 或 "speech-2.8-turbo" 或 None（无 minimax）
        → 任一 speaker 选 hd → 整 job 用 hd（已是现有规则，T2 必须 align）
  ↓
[NEW v4-F-v4-5] 完全不用 route db；独立 async_session_1 查：
        - Job.user_id → owner_id
        - 每个 speaker 的 voice 在 user_voices / voice_catalog 中的 chars_per_second_by_model
        - 关键：minimax voice 的 lookup model_key = final_minimax_model（不是 speaker 自己选的）
        - cosyvoice voice：第一阶段不参与 calibration（T0-C 限定 MiniMax）
        - volcengine voice：同上
  ↓
[v4] 关闭 async_session_1
  ↓
计算 missing voices：(provider, voice_id, final_model_key) tuple 中 by_model[final_model_key] NULL 的
  ↓
[NEW v4-F-v4-6] asyncio.wait(tasks, timeout=50.0, return_when=ALL_COMPLETED)
        - 每个 task 自带 60s total budget（T0-C）
        - max 4 并发（用 semaphore 控制）
        - 50s 后 return — 但 NOT cancel pending tasks
        - 给 pending tasks 加 done_callback：**仅 log + metrics**（task 自身在 factory 里已经走 `_do_calibrate_in_new_session` 写过 DB；done_callback 不再重复写库——v4.2 codex F-v4.2-4）
  ↓
proxy_request → Job API（用 route db；这里不再有外部等待）
  ↓
sync Gateway DB（用 route db）
  ↓
return response（含 done outcomes + still_running voice ids 供 UI 展示）
```

**Payload 提取规则（v4 修订：codex F-v4-1 修正字段名 + F-v4-2 加 final-model 派生）**:

`speakers[]` 实际 payload 字段命名（review submit 侧，输入别名 — 与前端 `voiceSelection.ts:23-29` 一致）：

| 输入字段 | 别名值示例 | 内部规范值 |
|---|---|---|
| `tts_provider` | `minimax` / `cosyvoice` / `volcengine` | 已是 canonical lowercase |
| `voice_id` | provider-side voice id | 直接用 |
| `minimax_model` | `hd` / `turbo` (only if tts_provider=minimax) | 经 `_aggregate_quality_tier_from_speakers` 升到 job-level → `speech-2.8-hd` / `speech-2.8-turbo` |
| `voice_source` | `library` / `cloned` / etc. | 决定 `scope`（CalibrationKey 字段） |

**v4 关键修正**：
- v3 写 `provider`，**实际是 `tts_provider`**（codex F-v4-1）。preflight 读 `tts_provider`。
- v3 按 speaker 自选的 `minimax_model` 校准，**实际应按 job-level final model 校准**（codex F-v4-2）。Speaker A 选 turbo + Speaker B 选 hd → final_minimax_model = `speech-2.8-hd` → A、B 两个 voice 都校准 hd model 的 CPS。

未填 `voice_id` 的 speaker 跳过；未识别的 `tts_provider` 跳过 + log warn。

**风险评估（v3 修订）**:

| 维度 | 评估 |
|---|---|
| 付费 API 合规 | ⚠️ submit 是用户显式动作，但 calibrate 是"被夹带"。**必须** UI 显式告知 "正在校准音色语速…(1/2)" + 时间预估 + 取消按钮。已校准过的 voice 不重复触发（T0-B in-flight + by_model[model_key] DB 检查双保险） |
| UX 延迟 | 单 voice ~30s（T0-C 60s 上限但通常不会跑满），多 voice 并发限 4，**全 batch 50s 硬上限**——超时强行放行进 fallback。比 v1 写的 25s 现实，比 v2 写的"不限"安全 |
| 失败模式 | calibrate 失败 / batch timeout → 走 default CPS + Post-TTS 反推。**永不阻断** submit。前端 UI 显示 "部分音色未能校准，已降级"——不让用户以为 submit 卡死 |
| 并发 / race | T1 + T2 可能 race（克隆完立刻去 review）。T0-B 5 元组 inflight 去重；前端在打开 review UI 时先 GET 一次 voice metadata 命中显示已校准状态 |
| Rate limit | T0-A budget；review submit 平均 1-3 voices × 1-2 model_key 不会撞 5/min。但 submit 撞了视为 calibrate fail → fallback，submit 仍放行 |
| **Proxy 顺序（codex v1 F5 + v3 F6 双护栏）** | calibrate 必须在 [`proxy_request:1608`](../../gateway/job_intercept.py:1608) 之前完成 OR batch timeout 已 fire。"do NOT block proxy on result" 措辞**已删除**（v3 F6）；新约束："block proxy with hard 50s upper bound" |
| **DB session（codex F10）** | 短 session 查 Job + 批量查 voice CPS → 关闭 session → 跑 calibrate（自建短 session 写）→ 重开短 session 进 proxy / sync。**绝不**持有 request session 等待 50s 外部 TTS |
| **Function 签名（codex F7）** | 当前 `(request, job_id, db)` 没 user 参数。v3 改造：第一步从 `Job.user_id` 反查 owner_id（job 已经在 db 里）。无需改 route handler 签名 |
| 观测 | log `[auto-calibrate-review-submit] job=X user=Y voices=[(prov,vid,model)...] hit=N miss=M timing=Yms outcome=ok/timeout/partial`；admin 看板按 outcome 分桶 |
| 回滚 | env `AVT_AUTO_CALIBRATE_ON_REVIEW_SUBMIT=true/false`，false 即关 |

**实施要点（v4 修订）**:

1. **前置依赖**：T0 全部完成 + T3 存量批量校准跑过（让 voice_catalog 多数库音色已校准，T2 多数 fast pass）
2. `_approve_voice_selection_with_quality_sync` 入口（[`job_intercept.py:1574`](../../gateway/job_intercept.py:1574)）改造（**v4.1 codex F-v4.1-2 + F-v4.1-3**）：
    ```python
    async def _approve_voice_selection_with_quality_sync(
        request: Request,
        job_id: str,
        db: AsyncSession,
    ) -> Response:
        body_bytes = await request.body()
        try:
            payload = json.loads(body_bytes) if body_bytes else {}
        except Exception:
            payload = {}
        speakers = payload.get("speakers") if isinstance(payload, dict) else []
        if not isinstance(speakers, list):
            speakers = []

        # v4 F-v4-2: derive job-level final MiniMax model FIRST.
        _quality_tier, final_minimax_model = _aggregate_quality_tier_from_speakers(speakers)

        # v4.1 F-v4.1-2: route `db` was already used by _verify_job_ownership
        # (job_intercept.py:1128 → 2633 SELECT). Release the connection back
        # to the pool BEFORE the 50s preflight wait. SQLAlchemy `rollback()`
        # ends the implicit transaction and releases the connection.
        if env_var_enabled("AVT_AUTO_CALIBRATE_ON_REVIEW_SUBMIT", default=False):
            await db.rollback()  # release connection — pool gets it back

        calibration_outcomes: list[dict] = []
        if env_var_enabled("AVT_AUTO_CALIBRATE_ON_REVIEW_SUBMIT", default=False):
            # v4 F-v4-5 + v4.1 F-v4.1-2: pre-flight uses INDEPENDENT sessions only.
            calibration_outcomes = await _pre_flight_calibrate_voices_independent(
                job_id=job_id,
                speakers=speakers,
                final_minimax_model=final_minimax_model,
                batch_total_timeout_seconds=50.0,
            )

        # By the time we reach here, calibration is either done or scheduled
        # to keep running in background. route `db` is fresh for proxy + DB
        # sync (the existing flow continues to use it).
        response = await proxy_request(...)  # line 1608 unchanged
        # ... existing DB sync ...

        response = _inject_calibration_outcomes_into_response(response, calibration_outcomes)
        return response
    ```

3. `_pre_flight_calibrate_voices_independent()` 结构（**v4.1 修订 codex F-v4.1-3**——拆 target build vs CPS query）：
    ```python
    async def _pre_flight_calibrate_voices_independent(
        *,
        job_id: str,
        speakers: list[dict],
        final_minimax_model: str | None,
        batch_total_timeout_seconds: float = 50.0,
    ) -> list[dict]:
        """v4.1: targets build → batch CPS query → close session → wait/launch.

        v4 had a bug where _build_calibration_targets returned (key, None) but
        downstream `_has_cps_in_by_model(None, ...)` checked None. v4.1
        separates target listing from CPS lookup cleanly.
        """
        # Phase 1: parse speakers → list of CalibrationKey only (no row).
        targets: list[CalibrationKey] = _build_calibration_keys(
            speakers=speakers,
            owner_id_placeholder="__will_resolve_in_session__",  # filled later
            final_minimax_model=final_minimax_model,
        )
        # owner_id resolved in session below.

        # Phase 2: open INDEPENDENT short session, resolve owner + batch query CPS.
        async with async_session() as db_query:
            job_row = (await db_query.execute(
                select(Job).where(Job.job_id == job_id)
            )).scalar_one_or_none()
            owner_id = str(job_row.user_id) if job_row and job_row.user_id else None
            if owner_id is None:
                return []

            # Re-key targets with the resolved owner_id (was placeholder).
            targets = [
                CalibrationKey(
                    scope=k.scope,
                    owner=owner_id if k.scope == "user" else "catalog",
                    provider=k.provider, voice_id=k.voice_id, model_key=k.model_key,
                ) for k in targets
            ]

            # Batch query: by scope, fetch chars_per_second_by_model for each.
            # Returns dict[CalibrationKey, dict[str, float] | None] (snapshot).
            by_model_snapshots: dict[CalibrationKey, dict | None] = (
                await _batch_query_by_model_snapshots(db_query, targets)
            )
        # db_query closed; the route db is also already rolled back.

        # Phase 3: compute missing list using snapshots (NOT None comparisons).
        missing_keys: list[CalibrationKey] = []
        already_calibrated: list[CalibrationKey] = []
        for key in targets:
            snap = by_model_snapshots.get(key)
            if snap is not None and key.model_key in snap and snap[key.model_key] is not None:
                already_calibrated.append(key)
            else:
                missing_keys.append(key)

        outcomes: list[dict] = [
            {"key": dataclasses.asdict(k), "status": "already_calibrated"} for k in already_calibrated
        ]
        if not missing_keys:
            return outcomes

        # Phase 4: launch tasks. Each factory self-writes via its own short
        # session inside the task. asyncio.wait with NO cancellation of pending.
        sem = asyncio.Semaphore(4)
        async def _bounded(key: CalibrationKey) -> CalibrationResult:
            async with sem:
                return await _run_calibration_for_review_submit(
                    key=key, owner_id=owner_id,
                )
        tasks: list[asyncio.Task] = [asyncio.create_task(_bounded(k)) for k in missing_keys]
        task_to_key = {t: k for t, k in zip(tasks, missing_keys)}

        done, pending = await asyncio.wait(
            tasks,
            timeout=batch_total_timeout_seconds,
            return_when=asyncio.ALL_COMPLETED,
        )

        for task in done:
            outcomes.append(_summarize_outcome(task, task_to_key[task]))

        # v4.1 F-v4.1-8: done_callback职责单一——只 log + metrics，不碰 DB。
        # The factory ALREADY wrote DB inside _do_calibrate_in_new_session()
        # before returning. The callback's job is purely observability.
        for task in pending:
            task.add_done_callback(
                lambda t, k=task_to_key[task]: _log_background_calibration_outcome(t, k)
            )
            outcomes.append({"key": dataclasses.asdict(task_to_key[task]), "status": "still_running"})

        return outcomes


    def _build_calibration_keys(
        *,
        speakers: list[dict],
        owner_id_placeholder: str,  # placeholder filled in async session
        final_minimax_model: str | None,
    ) -> list[CalibrationKey]:
        """v4.1 F-v4.1-3: returns CalibrationKey list ONLY (no row attempt).
        Row data fetched separately in batch later.

        v4 F-v4-1: read field `tts_provider` (not `provider`).
        v4 F-v4-2: minimax voices ALL get final_minimax_model.
        """
        keys: list[CalibrationKey] = []
        for sp in speakers:
            tts_provider = str(sp.get("tts_provider", "")).strip().lower()
            voice_id = str(sp.get("voice_id", "")).strip()
            voice_source = str(sp.get("voice_source", "")).strip().lower()
            if not voice_id or not tts_provider:
                continue

            if tts_provider == "minimax":
                if not final_minimax_model:
                    continue
                scope = "user" if voice_source == "cloned" else "catalog"
                owner = owner_id_placeholder if scope == "user" else "catalog"
                keys.append(CalibrationKey(
                    scope=scope, owner=owner, provider="minimax",
                    voice_id=voice_id, model_key=final_minimax_model,
                ))
            # cosyvoice / volcengine: T0-C-2 future scope — skip in phase 1
        return keys


    async def _batch_query_by_model_snapshots(
        db: AsyncSession,
        targets: list[CalibrationKey],
    ) -> dict[CalibrationKey, dict | None]:
        """v4.1 F-v4.1-3: batch fetch chars_per_second_by_model JSONB snapshots.

        Group by scope; one SELECT per scope. Returns snapshot dict per key
        (or None if the row doesn't exist).
        """
        result: dict[CalibrationKey, dict | None] = {}

        user_keys = [k for k in targets if k.scope == "user"]
        catalog_keys = [k for k in targets if k.scope == "catalog"]

        if user_keys:
            owner_id = user_keys[0].owner  # already resolved by caller
            voice_ids = [k.voice_id for k in user_keys]
            rows = (await db.execute(
                select(UserVoice).where(
                    UserVoice.user_id == owner_id,
                    UserVoice.voice_id.in_(voice_ids),
                )
            )).scalars().all()
            row_map = {r.voice_id: r for r in rows}
            for k in user_keys:
                row = row_map.get(k.voice_id)
                result[k] = (row.chars_per_second_by_model if row else None)

        if catalog_keys:
            # v4.2 codex F-v4.2-6: filter by provider + non-archived to avoid
            # cross-provider voice_id collisions and retired-voice pollution.
            # Group by provider so the catalog query doesn't span unrelated
            # rows; T2 phase 1 only handles minimax so this is one-shot.
            voice_ids = [k.voice_id for k in catalog_keys]
            rows = (await db.execute(
                select(VoiceCatalog).where(
                    VoiceCatalog.provider == "minimax",  # phase-1 limit
                    VoiceCatalog.voice_id.in_(voice_ids),
                    VoiceCatalog.archived_at.is_(None),
                )
            )).scalars().all()
            row_map = {r.voice_id: r for r in rows}
            for k in catalog_keys:
                row = row_map.get(k.voice_id)
                result[k] = (row.chars_per_second_by_model if row else None)

        return result
    ```

4. **`_run_calibration_for_review_submit(key, owner_id)`**：内部走 T0-A 配额 + T0-B atomic claim_or_join + T0-D model-aware；factory 自建短 session。与 T1 calibrate_after_clone 的差异**仅在于** `key.scope`（"catalog" 时不需要 owner_id 校验）。
5. 前端：submit 后显示 progress；最终响应带 outcomes（done + still_running）供 tooltip
6. env gate + 前端 feature flag 双保险

**测试策略（v4 修订）**:

- **unit**:
  - `test_review_submit_reads_tts_provider_field_not_provider`: payload 用 `tts_provider`, preflight 必须读对（**v4 codex F-v4-1 regression guard**）
  - `test_review_submit_calibrates_final_job_level_minimax_model_not_per_speaker`: speakers=[turbo, hd] → final=hd → 两个 voice 都校准 `speech-2.8-hd`（**v4 codex F-v4-2 regression guard**）
  - `test_review_submit_extracts_owner_from_job_user_id`: 验证从 Job 反查 user_id（**v3 codex F7 regression guard**）
  - `test_review_submit_checks_chars_per_second_by_model_not_scalar`: voice 有 scalar 但 by_model[final_model] NULL → 仍触发 calibrate（**v3 codex F3 regression guard**）
  - `test_review_submit_calibrates_only_missing_model_keys`: turbo 已校 hd 未校 + final=hd → 校准 hd
  - `test_review_submit_blocks_proxy_until_calibrate_done`: spy proxy_request → 必须在 calibrate 完成后才被调（**v1 codex F5 regression guard**）
  - `test_review_submit_proxy_fires_after_batch_timeout_without_canceling_pending`: 模拟 1 voice 60s + 1 voice 5s → batch 50s timeout → 第一个进 still_running，proxy 仍被调；50s 后 pending task 仍然完成并落库（**v3+v4 codex F-v4-6 regression guard**）
  - `test_review_submit_uses_independent_session_not_route_db`: spy on `async_session()` 调用次数 + 断言 route `db` 没在 calibration 期间被使用（**v4 codex F-v4-5 regression guard**）
  - `test_review_submit_pending_tasks_complete_in_background_after_return`: timeout 后 pending task **自身在 factory 内**完成 DB 写入；done_callback 只 log 不碰 DB（**v4 codex F-v4-6 + v4.2 codex F-v4.2-4 regression guard**）
  - `test_review_submit_skipped_when_env_disabled`: env=false → 跳过 calibrate, 直接 proxy
- **集成**：review submit with 2 voices (turbo 已校 / hd 未校), final=hd → 校准 hd; with no minimax voice → 直接 proxy
- **E2E（手工）**：真实 video, 全 NULL voices, 提交, 看 progress UI + 50s 上限不强制取消未完成

**估算**: 16-20 小时（v3 估 12-16h；v4 因 final-model 派生 + atomic claim_or_join + asyncio.wait + 完整独立 session 增加 4h）

**ROI**: **中-高**——覆盖 T1 没拦住的场景（库音色、复用历史克隆首次跑 video）
**风险**: 中（UX 复杂度 + 多个并发 session 管理 + batch timeout 边界 + done_callback 异常处理）

---

### 3.3 T3 — voice_catalog admin 可见性 + 批量校准

**位置**: gateway admin + ops 工具

**当前形态**: `gateway/scripts/calibrate_voice_speeds.py` 是 ops 手动跑的批量脚本；admin 后台**没有**任何 voice_catalog CPS 状态可见性。新音色加入库时（管理员配置）也没有自动校准。

**风险评估**:

| 维度 | 评估 |
|---|---|
| 付费 API 合规 | ✅ admin 显式触发，符合 |
| UX 延迟 | admin 任务，不影响用户路径 |
| 失败模式 | admin 看到失败 voice 列表，可重试 |
| 并发 | 批量限制并发到 5（避免撞 provider rate limit） |
| Rate limit | admin 走 internal API key，不受 user rate limit 约束 |
| 观测 | admin 看板：库音色总数 / 已校准 / 未校准 / 最近校准时间分布 |
| 回滚 | env `AVT_ADMIN_AUTO_CALIBRATE_ON_VOICE_ADD=true/false` |

**实施要点（v4 修订，model-aware + 第一阶段限 MiniMax）**:

1. admin 后台加一页 `/admin/voice-calibration`：列出未校准 voice。判定为 "`chars_per_second_by_model` IS NULL OR 缺指定 model_key"——比看 scalar 准：
    ```sql
    -- "缺 turbo 校准" 的 voice
    SELECT id, provider, voice_id FROM voice_catalog
    WHERE archived_at IS NULL
      AND (
        chars_per_second_by_model IS NULL
        OR NOT (chars_per_second_by_model ? 'speech-2.8-turbo')
      );
    ```
    ⚠️ 现有 `idx_vc_speed_calibrated` 是 `WHERE chars_per_second IS NOT NULL`（覆盖**已校准**行），对未校准查询**无用**：
    - **当前 catalog 规模**（< 几千行）：全表扫即可，亚秒返回
    - **未来规模 > 10k 行**：新增 migration `XXX_add_idx_vc_speed_uncalibrated`（具体 SQL 按需）
2. 一键"批量校准未校准"按钮：触发 background job 走 T0-A budget（admin 走 internal API key，不占用户配额）+ T0-B 5 元组 in-flight + T0-C bounded primitives + T0-D model_key 强制
3. **批量任务的 model_key 维度**：admin 选择"补齐 turbo" / "补齐 hd" / "全部 model 都补"。每个 model_key 是独立 calibration，独立 budget，独立 in-flight key
4. `gateway/scripts/calibrate_voice_speeds.py` 修订：默认遍历 `(voice, model_key)` 笛卡尔积；CLI 参数支持 `--model-keys speech-2.8-turbo,speech-2.8-hd`
5. 新音色加入 voice_catalog 时（admin 提交音色配置后）自动 enqueue calibrate（与 T1 同套机制）
6. 全量校准 cron（可选）：每月扫一次，给"过期 1 年"的音色重新校准（provider 升级模型可能改变 CPS）。需新加 `voice_catalog.speed_calibrated_at < now() - 1 year` 查询；该列已存在（migration 012）
7. **v4 范围限定（codex F-v4-7）**：T3 第一阶段**只覆盖 MiniMax 库音色**。CosyVoice / VolcEngine 的库音色 calibration 走 ops 批量脚本（不经过 calibrator helper 的 bounded primitive 改造，避免动 helper subprocess 协议）。等 T0-C-2 sub-task 完成后再扩 admin 后台覆盖范围

**估算**: 6-8 小时
**ROI**: 中（提升 admin ops 效率，减少 T2 的 fallback 命中率）
**风险**: 低

---

## 4. 推荐执行顺序

```
T0（基础设施）→ T1（克隆 hook）→ 观察 1 周 → T3（admin 可见 + 存量批量）→ 观察 + 数据 → T2（review submit）
```

**理由**:

1. **T0 必须最先做**：calibration budget + in-flight dedupe + 硬 timeout 是其余三个 task 的**正确性前置**。
   - 没有 T0-A（budget）：手动 endpoint + T1 + T2 可被同一用户在 console 里 spam，付费 API 失控。
   - 没有 T0-B（in-flight dedupe）：T1 enqueue + T2 同步等同 voice，并发 2 倍付费调用。
   - 没有 T0-C（硬 timeout）：T2 的"25s 后 fallback 放行" 是假承诺，线程内付费调用照跑。
2. **T1 在 T0 之后**：T0 已让"自动 calibrate"在 budget / dedupe / timeout 三个维度都安全；T1 在此基础上加 trigger。改动小、风险低、ROI 高、覆盖最常见的"用户克隆完立刻跑 video"场景。
3. **T3 在 T2 之前**：T3 把 voice_catalog 大部分库音色批量校准完，T2 的 fallback 命中率会大幅下降，T2 的 UX 复杂度也降低（多数 submit 不需要 wait）。
4. **T2 最后**：UX 路径最复杂，需要 T3 先把存量数据补齐才能验证"少数情况下才弹 progress UI"是否真的少数。同时 T2 是唯一引入"同步等待付费调用"的 task，必须在 T0-C 硬 timeout 已落实之后才合法。

---

## 5. 详细实施 checklist

### 5.0 T0 — 基础设施前置（先做，v3 修订）

**T0-A budget（v3 reserve/refund 顺序）**

- [ ] 1. `gateway/risk_control.py` 加 `reserve_voice_calibration` / `refund_voice_calibration`，config 阈值 `per-user 5/min, 30/day`（待 §7 决策点 2 拍板）
- [ ] 2. 接入手动 endpoint [`user_voice_api.py:281`](../../gateway/user_voice_api.py:281)：入口加 `reserve_voice_calibration`，**仅** "未发起 paid call" 的错误路径 refund（voice 不存在 / unsupported provider / 输入校验）；provider 5xx / synth timeout / 空音频**不** refund

**T0-B 5 元组 in-flight（v3 codex F1+F3）**

- [ ] 3. 新建 `gateway/voice_calibration_inflight.py`，定义 `CalibrationKey(scope, owner, provider, voice_id, model_key)` + `CalibrationInFlightRegistry`
- [ ] 4. 修改全部 caller（手动 endpoint / T1 / T2 / T3）走 `registry.claim_or_join(key)`（**v4.1 codex F-v4.1-5**：旧名 `get_or_start` / `start_calibration` 全部替换）；joiner 路径**不** reserve budget；joiner `await asyncio.shield(future)`；starter `release(key, future)` 带 future identity

**T0-C bounded primitives（v3 codex F5）**

- [ ] 5. `voice_speed_calibrator.py`：
  - `_synthesize_minimax` 内 `_post_json(timeout_seconds=12.0, max_retries=1)`（替换 60s × 2）
  - `_measure_wav_duration_ms` 的 `subprocess.run` 加 `timeout=10`，捕获 `subprocess.TimeoutExpired` 抛 `RuntimeError("ffprobe timeout")`
  - `calibrate_voice()` 加 `total_timeout_seconds=60.0`：在每段开始**前** + bounded call 返回**后**两个 check point 检查预算；预算耗尽返 `CalibrationResult(ok=False, error_class="total_timeout", paid_call_count=<so_far>)`
  - **不**承诺打断已经在 `_post_json` 12s 等待中的调用
  - **v4.1 codex F-v4.1-7**：`paid_call_count` 在每次 synth 调用**前**递增（不是后），即使 synth 抛异常也保留计数
  - **v4.1 codex F-v4-4**：所有 exception 路径包成 `CalibrationResult`——`calibrate_voice` 永不向上抛

**T0-D model-aware data model（v3+v4.1 codex F-v4.1-1）**

- [ ] 6. `calibrate_voice()` 签名改为必传 `model: str`（去掉默认 fallback）；移除 `_DEFAULT_CALIBRATION_MODEL` 在自动化路径的使用，仅手动 endpoint 兜底用
- [ ] 7. `CalibrationResult` 加 `error_class: str`、`paid_call_count: int`、`model_key: str`
- [ ] 8. **v4.1 关键** `update_voice_speed_calibration()` 改成 **`SELECT ... FOR UPDATE` + merge** 模式（方案 A，见 §3.0 T0-D）：
  - 签名改为 `(db, voice_id: str, user_id: str, *, cps: float, model_key: str)`，内部 `with_for_update()` 重新 fetch row + 行锁 + merge JSONB + commit
  - **不再**接受 caller 预先 fetch 的 row 对象——避免 caller 持过时 row 误覆盖
  - voice_catalog 写入 helper 同套设计
- [ ] 9. 全 repo grep `update_voice_speed_calibration\|calibrate_voice`，所有调用改成传 `model_key` + 适配新签名
- [ ] **9.5 [v4.3 F-v4.3-2] 手动 endpoint 改造**：[`gateway/user_voice_api.py:281 calibrate_voice_speed`](../../gateway/user_voice_api.py:281) 必须遵循"不持 route DB 跨 paid call"原则——
  - 鉴权 + voice 存在性校验后立即 `await db.rollback()` 释放 route session
  - paid TTS 用 `asyncio.to_thread(calibrate_voice, ...)`
  - 写入用新 helper `update_user_voice_speed_calibration`（自建 `SELECT FOR UPDATE` session）
  - body 接 optional `model_key`（不传 = 双 model 并发，与 T1 行为对齐 — F-v4-8 的 v4 规则）

**T0 测试（v4.1 修订）**

- [ ] 10. 测试：
  - `test_calibration_budget_blocks_after_per_minute_limit`: 6 次连发 → 第 6 次 429
  - `test_calibration_budget_joiner_does_not_reserve`: starter + joiner 同 key，只 1 次 reserve（**v4 codex F-v4-3 regression guard**）
  - `test_calibration_no_refund_after_paid_call_count_gt_zero`: paid_call_count=2 + ok=False，不调 refund（**v4 codex F-v4-4 regression guard**）
  - **`test_paid_call_count_incremented_before_synth_attempt`**: mock synth 第一段抛异常 → `paid_call_count == 1`，caller 不 refund（**v4.1 codex F-v4.1-7 regression guard**）
  - `test_calibration_inflight_5tuple_key_isolates_models`: 同 voice 的 turbo + hd 是不同 key, 并发跑（**v3 codex F1 regression guard**）
  - `test_calibration_inflight_5tuple_key_isolates_users`: 同 voice 的 user_a + user_b 是不同 key
  - **`test_calibration_inflight_release_identity_check`**: starter A 异常释放 → 不误删 starter B 的同 key future（**v4.1 codex F-v4.1-6 regression guard**）
  - **`test_calibration_inflight_joiner_uses_shield_against_cancel`**: joiner task 被 cancel → 共享 future 不被取消，starter 仍能 set_result（**v4.1 codex F-v4.1-4 regression guard**）
  - **`test_concurrent_calibrate_turbo_and_hd_preserves_both_keys`**: 并发 2 task 同 voice 不同 model，commit 后 by_model 含 turbo + hd（**v4.1 codex F-v4.1-1 regression guard**）
  - **`test_manual_calibrate_endpoint_does_not_hold_route_db_across_paid_call`**: spy 上 `db.rollback()` / `db.close()` 调用顺序——必须在 `asyncio.to_thread(calibrate_voice, ...)` 之前；paid call 期间 route db 不应被 `db.execute(...)` 触达（**v4.3 codex F-v4.3-2 regression guard**）
  - `test_total_timeout_skips_remaining_texts_after_budget_exhausted`: fake clock，前 2 段累计 55s，段 3 入口 check 跳过；`paid_call_count == 2`（**v3 codex F5 regression guard**）
  - `test_ffprobe_timeout_does_not_hang`: mock ffprobe 卡住 → 10s 抛 TimeoutExpired
  - `test_calibrate_voice_writes_to_by_model_with_explicit_model_key`: 验证 `chars_per_second_by_model[model_key]` 被更新（**v3 codex F2 regression guard**）
- [ ] 11. 跑 `tests/test_voice_speed_calibrator.py` + `tests/test_p2_23_voice_probe_rate_limit.py` + 新加的 T0 测试
- [ ] 12. commit T0；codex review
- [ ] 13. 部署：upload + force-recreate aivideotrans-gateway（env 变了）
- [ ] 14. 部署后 smoke：跑一次 manual calibrate-speed，验证 budget 计入；admin 看板查计数；验证 by_model 写入正确

### 5.1 T1 — 克隆 hook（T0 完成后，v3 修订：双 model 并发）

- [ ] 1. 写 characterization tests：现有的 voice clone + add_user_voice 的成功路径不变（pin 当前行为）
- [ ] 2. 在 `gateway/user_voice_service.py` 或新建 `gateway/voice_calibration_hook.py` 中加：
   ```python
   async def calibrate_after_clone(
       *, voice_id: str, user_id: str, provider: str, model_key: str
   ) -> None
   ```
   - **只接 primitive 参数**（v2 codex F2 要求）
   - 内部 `async with async_session() as db_bg:` 重新打开 session
   - in-flight check **先于** budget reserve（v3 codex F4）
   - 走 T0-A budget + T0-B 5 元组 in-flight + T0-C bounded + T0-D 写 by_model[model_key]
   - 包 try/except；所有失败 log + 静默返回
   - log 前缀 `[auto-calibrate-clone] voice=X model=Y`
- [ ] 3. 在 [`voice_selection_api.py:514`](../../gateway/voice_selection_api.py:514) 之后加（**v3 双 model 并发**）：
  ```python
  if env_var_enabled("AVT_AUTO_CALIBRATE_AFTER_CLONE", default=True):
      for model_key in ("speech-2.8-turbo", "speech-2.8-hd"):
          asyncio.create_task(
              calibrate_after_clone(
                  voice_id=clone_result,
                  user_id=user_id,
                  provider="minimax",   # canonical, not "minimax_tts"
                  model_key=model_key,
              )
          )
  ```
  **注意**：传 primitive，不传 db / row object
- [ ] 4. 添加 env `AVT_AUTO_CALIBRATE_AFTER_CLONE`（默认 true）+ docker-compose.yml 显式列出
- [ ] 5. 写测试（含 v2/v3 codex regression guards）：
  - `test_clone_enqueues_two_calibrate_tasks_for_turbo_and_hd`: mock calibrate_voice → 验证被调 2 次, model_key 分别 turbo/hd（**v3 codex F2 regression guard**）
  - `test_clone_succeeds_when_calibrate_fails`: calibrate 抛异常 → clone 仍 200
  - `test_clone_skips_calibrate_when_env_disabled`: env=false → 0 次 calibrate
  - `test_clone_calibrate_skips_on_budget_exhausted`: T0-A budget 满, calibrate 跳过 + log
  - `test_clone_calibrate_inflight_joiner_does_not_reserve_budget`: 第二个并发 caller 不消耗 budget（**v3 codex F4 regression guard**）
  - `test_clone_calibrate_no_refund_after_paid_call_count_gt_zero`: provider 5xx 后不 refund（**v3 codex F4 regression guard**）
  - `test_clone_calibrate_uses_new_db_session_not_request_session`: 不复用 request session（**v2 codex F2 regression guard**）
  - `test_clone_calibrate_writes_to_chars_per_second_by_model_not_only_scalar`: 验证 by_model[model_key] 被更新（**v3 codex F2 regression guard**）
- [ ] 6. 跑 T0 测试 + T1 测试 + `tests/test_user_voice_*.py` 全过
- [ ] 7. commit T1；Codex review 一轮
- [ ] 8. 部署：upload + force-recreate aivideotrans-gateway（env 变了）。**注意**：T1/T2/T3 全部在 gateway 容器
- [ ] 9. 部署后 smoke：克隆 1 个 MiniMax voice, 等 60s, `curl /gateway/user-voices/{id}` 看 `chars_per_second_by_model` 含 turbo + hd 两个 key

### 5.2 T2 — review submit pre-flight（**T2 开工前的硬前置**，v4.3 codex F-v4.3-1）

T2 详细 checklist 在 T1 + T3 落地后再写（§3.2 已有完整设计）。**但 T2 开工前必须先解决这一条 prerequisite**：

- [ ] **P1 [v4.3 F-v4.3-1] 修 `voice_source` 误判 user/catalog**：当前前端 `VoiceSelectionPanel.tsx:331` / `:475` 把"我的音色"复用项也写成 `voiceSource: 'catalog'` 提交。T2 `_build_calibration_keys` 不能只靠 payload 字段判 scope。**推荐后端 fallback 方案**：
  - MiniMax voice_id 先按 `(owner_id, voice_id)` 批量查 `user_voices`
  - 命中 → user scope（owner = user_id）
  - 未命中 → fallback 到 catalog scope
  - 加 regression test：模拟"用户复用历史克隆音色，前端 payload 写 catalog"→ T2 路由到 user_voices 校准
  - 优于改前端：约束面小、不依赖前端发版周期

T2 checklist 其余项（流程图实现 / batch_query_snapshots / asyncio.wait + done_callback / 测试）按 §3.2 v4.2 设计落地，详细 checklist 待 T1 + T3 完成后再补。

### 5.3 T3 — admin 后台批量

T3 详细 checklist 同样待 T2 之前补——主要是 §3.3 已有的设计落地：admin 页 + 批量 button + 新音色入库 hook + cron。

---

## 6. 通用回滚预案

**任何阶段（T0-T3）都必须有 env-var kill switch**：

| 阶段 | env var | 设 false 时 |
|---|---|---|
| T0-A | `AVT_CALIBRATION_BUDGET_ENABLED` | 不限流（保留 v1 现状）。**生产建议保持 true** |
| T0-B | （永久开启） | in-flight dedupe 防 race，关闭即 bug；不应有 kill switch |
| T0-C | （永久开启） | 硬 timeout 防 hang；关闭等于无界等待，不应回滚 |
| T1 | `AVT_AUTO_CALIBRATE_AFTER_CLONE` | 克隆完不自动校准（退化到现状） |
| T2 | `AVT_AUTO_CALIBRATE_ON_REVIEW_SUBMIT` | submit 不自动校准（退化到现状） |
| T3 | `AVT_ADMIN_AUTO_CALIBRATE_ON_VOICE_ADD` | admin 加音色不自动校准 |

**应急流程**: 改 `.env` → `docker compose up -d --force-recreate aivideotrans-gateway`（T0/T1/T2/T3 全部在 gateway 容器，不是 app；与 P2-17a 不同）

---

## 7. 决策点（待审核）

执行前需要 product / 你的明确确认：

1. **执行顺序**：T0 → T1 → T3 → T2（不变）。推荐：是
2. **calibration budget 阈值**：T0-A `reserve_voice_calibration` 默认 `per-user 5/min, 30/day`。**v3 提醒**：T1 一次 clone 消耗 2 个配额（turbo + hd 各 1）；T2 一次 review submit 消耗 N voices × M missing models 个配额。**待拍板**：5/min 够吗？需要拉到 8/min 让 T2 稍宽松？
3. **T0-C 单段 timeout 值**：推荐 12s（替换当前 60s × max_retries=2）。理由：MiniMax 试听 endpoint 实测 p95 < 10s。**待拍板**：T0-C 总预算 60s 是否够 3 段文本？还是给 90s 更稳？
4. **T1 默认是否双 model 并发校准？**（v3 新增）推荐：是。理由：用户在 review 阶段切档（turbo ↔ hd）应当零延迟；多花 ~$0.0001/clone 可忽略。**待拍板**：是否要给前端一个"只校准 turbo / 只校准 hd / 双校准" 三选项让用户决？我推荐**不给**——多余的 UI 决策没必要，默认双校准。
5. **T2 是否同步阻塞等待，硬上限多少？**（v3 修订）推荐：阻塞等待，**全 batch hard upper 50s**。超时后强制放行进 fallback。前端必须 progress UI + 取消按钮。**待拍板**：50s 还是 60s？
6. **T2 撞 batch timeout 后已发起的付费调用如何处理？**（v3 新增）推荐：用 `asyncio.shield` 让已发起的 task 落地写入 DB（下次同 voice 直接 hit），但不阻塞 batch return。**待拍板**：是。
7. **T3 是否对存量库音色做一次性大批量补全？** 推荐：是
8. **是否新增 `idx_vc_speed_uncalibrated` 部分索引？** 推荐：先不加，catalog < 10k 行时全表扫够
9. **CPS 过期机制？** 推荐：先不加
10. **calibration 成本归属**（v3 新增 R2 建议）：推荐：记入 system calibration cost，**不**直接归到触发它的视频 job 毛利率上（除非该 job 是 T2 直接触发，可考虑选项 attribution）。**待拍板**：admin 看板是否需要按 user 分桶 calibration 累计成本？
11. **scalar `chars_per_second` 字段命运**（v3 新增）：v3 写 path 改成只写 by_model；scalar 字段自动更新为 cross-model mean 仅作 read-path 兜底。**待拍板**：是否在 T0-D 完成后开 followup 任务把 scalar 列彻底废弃（migration 直接 drop column）？我倾向**先保留**——removing 是不可逆变更，等 by_model 数据稳定后再做。
12. **修复后是否补 retry 收敛护栏？** 推荐：T0+T1 完成观察期后再评估。归入 P2-17 后续优化清单

---

## 8. References

- 触发事件：2026-05-09 `job_31a8f5c357c64aec856f84bf180a6d65` segment_070 strict_retry_reason API mismatch + 重写循环不收敛 → fix `78eea96`
- Calibration 核心：[`gateway/voice_speed_calibrator.py`](../../gateway/voice_speed_calibrator.py)（T0-C 改造目标：line 124 MiniMax synth timeout / line 161 ffprobe 无 timeout）
- 单音色 endpoint：[`gateway/user_voice_api.py:281 calibrate_voice_speed`](../../gateway/user_voice_api.py:281)（T0-A 接入目标；当前 line 218/316 用 `_DEFAULT_CALIBRATION_MODEL[provider]` 默认 model，T0-D 改为必传 model_key）
- T1 hook 落点：[`gateway/voice_selection_api.py:514`](../../gateway/voice_selection_api.py:514)（`add_user_voice` 之后；v3 双 model 并发 enqueue）
- T2 hook 落点：[`gateway/job_intercept.py:1166 route`](../../gateway/job_intercept.py:1166) → [`_approve_voice_selection_with_quality_sync():1574`](../../gateway/job_intercept.py:1574)（v3 line 已核对；calibrate **必须**在 line 1608 `proxy_request` 之前完成或 50s batch timeout 触发）
- CPS read path（已 model-aware）：[`src/services/tts/voice_speed_catalog.py:124-153 resolve_chars_per_second`](../../src/services/tts/voice_speed_catalog.py:124)（priority: `by_model[tts_model]` → scalar → None）
- 批量工具：[`gateway/scripts/calibrate_voice_speeds.py`](../../gateway/scripts/calibrate_voice_speeds.py)（T3 复用；v3 修订支持 `--model-keys` 参数）
- DB schema：
  - migration `012_add_voice_speed_calibration`（voice_catalog；`chars_per_second_by_model` JSONB；index 是 `WHERE chars_per_second IS NOT NULL`，**不**支持未校准查询）
  - migration `013_add_user_voice_speed_calibration`（user_voices；同结构）
- Existing rate-limit pattern（T0-A 模板）：[`gateway/risk_control.py reserve_voice_probe / refund_voice_probe`](../../gateway/risk_control.py)（P2-23 模式；T0-A 新增独立的 `reserve_voice_calibration` / `refund_voice_calibration`，**不复用** voice probe budget）
- Existing async_session helper（T1 background + T2 短 session 用）：[`gateway/database.py:97 async_session`](../../gateway/database.py:97)
- Pipeline 反推：[`src/pipeline/process.py:7384 _calibrate_tts_duration`](../../src/pipeline/process.py:7384)
- Pre-TTS rewrite char bounds：[`src/pipeline/process.py:5791 _pre_tts_rewrite_char_bounds`](../../src/pipeline/process.py:5791)
- Pre-TTS rewrite CPS lookup：[`src/pipeline/process.py:5237 speaker_chars_per_second`](../../src/pipeline/process.py:5237) → 最终走 `voice_speed_catalog.resolve_chars_per_second(voice_id, provider, tts_model=...)`（pipeline 必须传 model）
- 关联（不归本 plan）：Pre-TTS rewrite heuristic 不准的设计 limitation 见 P2-17 plan 的"长期改进 A/B/C"
- CLAUDE.md 付费 API 自动调用约束（"用户显式动作绑定"语义）

---

**v3 line number 核对**（2026-05-09）：

| 引用 | v1/v2 写的 | v3 实际 |
|---|---|---|
| `review/voice-selection/approve` route | 1185-1186 | **1166-1167** |
| `_approve_voice_selection_with_quality_sync` def | 1574 | **1574** ✓ |
| `proxy_request` in approve func | 1608 | **1608** ✓ |
| `voice_selection_api.py add_user_voice` block | 503-516 | **501-516** ✓（v3 已纠到 514） |
| `user_voice_api.py calibrate_voice_speed` | 281 | **281** ✓ |
| `_DEFAULT_CALIBRATION_MODEL` 使用点 | — | 218 + 316 |

---

**End of plan.** 不要在审核通过前动代码。
