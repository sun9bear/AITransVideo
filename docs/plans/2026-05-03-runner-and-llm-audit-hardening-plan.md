# 剪映 Runner 与 LLM Attempt Audit 加固方案

> Status: Draft
> Last updated: 2026-05-03
> Scope: 在不接入 Google ADK、不重写主 pipeline 的前提下，优先加固剪映草稿按需生成状态机与付费 LLM 调用审计链路；复用现有 `JobEvent` / `UsageMeter`，不新建第二套事件系统。

## 1. 结论

短期最有价值的优化不是接 Google ADK 2.0，也不是把整条 pipeline 改成新的 workflow 框架，而是把现有确定性系统的两个薄弱点补牢：

1. **剪映草稿 Runner 加固**：`JianyingDraftRunner` 已经是产品交付链路的一部分，且当前 daemon thread + 进程重启会留下 orphan `running` 状态。这里有明确现存 bug，改动边界小，收益立刻可验证。
2. **LLM Attempt Audit**：项目有付费 API 硬约束，现有 `UsageMeter` 已经能记录 LLM/TTS 用量，但 LLM fallback / retry / invalid output 的失败 attempt 还不够结构化。这里要补“每次尝试”的审计，不只记录最终成功。

其他方向保持克制：

- Workflow events 不另立系统，只扩现有 `JobEvent` 使用点。
- `llm_registry.py` 暂不大改，等模型升级或真实 drift 再按需加字段。
- 全 pipeline checkpoint/resume 暂缓，等 attempt audit 产生失败分布数据后再决定是否值得做。

## 2. 当前事实基线

### 2.1 已有 JobEvent 系统

项目已经有 `JobEvent`，不要再新增 `WorkflowEventSink`：

- `src/services/jobs/events.py`
  - `EVENT_TYPE_LOG`
  - `EVENT_TYPE_STATUS`
  - `EVENT_LEVEL_INFO`
  - `EVENT_LEVEL_WARN`
  - `EVENT_LEVEL_ERROR`
  - `EVENT_LEVEL_CRITICAL`
  - `download.redirect.r2`
  - `download.fallback.local`
  - `download.local.direct`
- `src/services/jobs/store.py`
  - `append_event(...)`
  - `load_events(...)`
  - 落盘路径：`{jobs_dir}/{job_id}.events.jsonl`
- `gateway/storage/event_log.py`
  - gateway 侧专门手写 download event writer，避免 import `services.jobs.events` 拉入 pipeline / pydub 依赖。

重要约束：

- Job API 内部要优先用 `JobStore.append_event(...)`。
- 直接构造 `JobEvent` 时必须传 `created_at`。建议统一通过小 helper 注入 UTC ISO timestamp，避免各调用点复制时间生成逻辑。
- Gateway 不能 import `services.jobs.events`。
- 如果新增 **Gateway 也会写入** 的 event type，必须同步维护 gateway allow-list 和回归守卫。
- 如果只是 Job API 内部使用 `EVENT_TYPE_STATUS` / `EVENT_TYPE_LOG`，不需要改 gateway writer。
- `EVENT_LEVEL_CRITICAL` 已经有语义：needs-ops-intervention。剪映 runner 的 orphan/stale running 回收失败应按这个约定进入 critical，而不是普通 error。

### 2.2 已有 UsageMeter

项目已经有 `UsageMeter` append-only sidecar：

- 落盘路径：
  - `{project_dir}/metering/usage_events.jsonl`
  - `{project_dir}/metering/usage_summary.json`
- `UsageMeter.record_llm(...)` 已有字段：
  - `task`
  - `provider`
  - `model`
  - `model_id`
  - `phase`
  - `attempt_label`
  - `success`
  - `error`
  - 文本长度和估算 token

关键缺口不是“没有用量系统”，而是：

- 成功 attempt 被记录得更多，失败 attempt / fallback decision 仍偏散。
- provider error、schema/JSON/validator failure、长度约束 failure 没有统一分类。
- fallback 决策多靠 `print`，容器重建后 docker logs 会丢，不满足付费 API 可审计要求。

### 2.3 已有 JianyingDraftRunner

当前剪映草稿生成链路：

- 前端在 Studio 成功任务结果页触发 `generate-jianying-draft`。
- Gateway 做 ownership proxy。
- Job API 的 `JianyingDraftRunner` 做 Studio-only / succeeded-only gate。
- Runner 状态为 `idle / running / succeeded / failed`。
- Runner 用 daemon thread 后台生成。
- 启动时 `reap_stale()` 把 stale `running` 标成 failed。

关键缺口：

- 进程重启会留下 orphan `running`，现在靠 stale reaper 兜底，不是设计上无 orphan。
- 没有 artifact fingerprint，无法严谨判断“同一个输入是否可复用 zip”。
- 并发触发只靠状态字段，缺少跨进程/多 worker 的 CAS 或 lock。
- `running` 内部没有 sub-step，失败时只能看到粗粒度 error。

## 3. 优先级

### P0：剪映 Runner 加固

优先理由：

- 剪映草稿是当前产品交付物，不是附加功能。
- 有明确现存 bug：daemon thread 重启 orphan `running`。
- 改动边界集中在 `src/services/jobs/jianying_draft_runner.py`、`models.py`、`store.py` 附近。
- 不触主 pipeline，测试闭环短。

### P1：LLM Attempt Audit

优先理由：

- 直接服务 `CLAUDE.md` 的付费 API 硬约束。
- retry/fallback/invalid output 都可能已经产生费用。
- 已有 `UsageMeter` 和 metering writeback，可在现有结构上补记录。

### P2：Workflow Events 顺手扩展

原则：

- 不单独立项做“全 pipeline event 改造”。
- P0 / P1 改到哪里，就在那里补 `JobEvent`。
- 能用 `EVENT_TYPE_STATUS` / `EVENT_TYPE_LOG` 就不新增 event type。

### Deferred：Registry 收敛与全 pipeline checkpoint/resume

暂缓理由：

- `llm_registry.py` 现在基本够用，不要一次性加满字段。
- checkpoint/resume 改动面大，应先看 audit 数据判断失败集中在哪里。

## 4. Phase A：剪映 Draft Runner 加固

### A1. 目标

把 `JianyingDraftRunner` 从“daemon thread + stale 兜底”升级为：

- 可跨进程避免重复生成。
- 可根据 artifact fingerprint 幂等复用。
- 进程重启后能收敛到明确状态。
- 失败时有 sub-step 和错误分类。
- 对前端保持原有 `idle/running/succeeded/failed` 公共状态兼容。

### A2. 非目标

本阶段不做：

- 不改剪映 writer 的草稿 JSON 格式。
- 不接任务队列服务。
- 不引入 Celery/RQ/Redis。
- 不改变前端交互模型。
- 不自动生成渲染 MP4。
- 不把剪映草稿恢复逻辑扩展成全 pipeline resume。

### A3. 数据模型建议

在 `JobRecord` 增加最小字段：

```python
jianying_draft_fingerprint: str | None = None
jianying_draft_attempt_id: str | None = None
jianying_draft_substep: str | None = None
```

字段语义：

- `jianying_draft_fingerprint`
  - 当前 succeeded zip 对应的输入指纹。
  - 用于判断再次点击时能否直接复用。
- `jianying_draft_attempt_id`
  - 本次 running 尝试 ID。
  - 用于 stale/orphan/lock/event 关联。
- `jianying_draft_substep`
  - 当前内部子步骤，供 status API 和 admin 诊断。
  - 对前端可选展示，不影响旧 UI。

可选字段，不建议第一轮加入：

- `jianying_draft_lock_owner`
- `jianying_draft_engine_version`
- `jianying_draft_artifact_size`

这些可以放在 `JobEvent.payload` 或 compatibility report 里，避免 JobRecord 膨胀。

真源关系必须写清：

- **并发控制真源**：现有跨平台 file lock，见 `src/services/_file_lock.py::file_lock`。是否允许启动新生成尝试，必须在 lock 保护的 read-modify-write 临界区内判断。
- **公共状态真源**：`JobRecord.jianying_draft_status` 仍是 API / UI 的公共持久状态。
- **诊断派生字段**：`attempt_id` / `substep` / `fingerprint` 是 public state 和 audit 的派生事实，不应单独作为并发控制依据。

换句话说，不能只因为 `JobRecord.jianying_draft_status != "running"` 就直接起线程；必须先进入 file lock 临界区重读 job，再决定是否 transition。

### A4. Artifact Fingerprint

新增内部函数：

```python
def _compute_jianying_fingerprint(job: JobRecord, user_draft_root: str | None) -> str:
    ...
```

建议输入：

- 剪映草稿真正消费的关键 artifact 内容 hash：
  - `source.original_video` 或实际进入草稿的视频素材。
  - `editor.dubbed_audio_complete` 或实际进入草稿的配音音频。
  - `editor.subtitle_cues` / `editor.subtitles` / 实际进入草稿的字幕输入。
  - `editor.ambient_audio`，如果进入草稿。
- 从 artifact index / manifest 读取上述 artifact 的稳定路径后，对文件内容做 sha256。
- 规范化后的 `user_draft_root`，无 root 时用空字符串。
- `JianyingDraftBackend` / writer 的版本常量。
- `editor.jianying_draft_zip` artifact schema version。

不要把绝对临时输出目录写进 fingerprint。fingerprint 应代表“相同业务输入是否产生等价草稿”，不是代表某次运行路径。

不要直接把完整 `manifest.json` 文件 sha256 放进 fingerprint。manifest 可能包含时间戳、mtime、排序差异或非剪映输入相关字段，整文件 hash 会让相同业务输入在重跑后得到不同 fingerprint，导致 zip 复用失效。只有两种情况可接受：

- 使用 artifact index 中关键文件的 **content hash**。
- 或先 normalize manifest，剔除 timestamps / mtime / transient fields 并稳定排序后，只保留剪映输入相关字段再 hash。

建议实现：

```python
payload = {
    "artifact_hashes": {
        "source_video": _sha256_file(source_video_path),
        "dubbed_audio": _sha256_file(dubbed_audio_path),
        "subtitle_input": _sha256_file(subtitle_input_path),
        "ambient_audio": _sha256_file(ambient_audio_path) if ambient_audio_path else "",
    },
    "user_draft_root": normalized_root or "",
    "backend_version": JIANYING_DRAFT_BACKEND_VERSION,
    "writer_version": JIANYING_DRAFT_WRITER_VERSION,
    "artifact_schema": 1,
}
return sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
```

### A5. 幂等策略

触发时：

1. 校验 `user_draft_root`。
2. 读取 job。
3. gate：`service_mode == studio`。
4. gate：`job.status == succeeded`。
5. 计算 fingerprint。
6. 如果 `jianying_draft_status == succeeded`：
   - `job.jianying_draft_fingerprint == fingerprint`
   - `job.jianying_draft_zip_path` 存在且文件可读
   - 则直接返回 existing artifact。
7. 如果 succeeded 但 fingerprint 不同：
   - 进入重建流程。
8. 如果 running：
   - 如果 attempt lock 仍有效，返回 running。
   - 如果 lock 不存在或 stale，先执行 orphan recovery，再决定是否重启。

### A6. 跨进程 Lock / CAS

当前 `JobStore` 是 JSON 文件存储，没有 DB `SELECT FOR UPDATE`。第一阶段不要再造新的 `os.O_CREAT | os.O_EXCL` lock 格式，应复用项目已有跨平台锁：

```text
src/services/_file_lock.py::file_lock
```

建议 lock target：

```text
{jobs_dir}/_locks/jianying_draft/{job_id}.run
```

`file_lock(path)` 会在该路径旁创建 `.lock` sidecar，并使用 `threading.RLock + fcntl/msvcrt` 覆盖线程内重入与跨进程互斥。

使用方式：

- `trigger()` 在 lock 临界区内重读 job、计算 fingerprint、判断是否可复用、写入 running 状态和 `attempt_id`。
- 后台生成线程也应在同一 job lock 下执行生成，确保跨进程不会同时生成同一 job 的草稿。
- `attempt_id`、`pid`、`started_at`、`fingerprint` 不写自定义 lock file 格式，写到 `JobRecord` 和 `JobEvent.payload`。
- 如果实现需要非阻塞探测 lock 是否被其他进程持有，应该在 `_file_lock.py` 内扩展 `try_file_lock`，不要另起一套 lock 工具。

stale / orphan recovery：

- stale 阈值必须与 `JianyingDraftRunner.STALE_THRESHOLD_SECONDS` 对齐，当前为 1800 秒。
- 进程崩溃后 OS lock 会释放，但 `JobRecord` 可能仍是 `running`。
- reaper 启动时应在 file lock 临界区内处理 stale `running`，避免与正在生成的进程竞争。
- 接管前先读取 job 状态和 artifact。
- 如果 zip 已存在且 fingerprint 匹配，收敛为 succeeded。
- 否则收敛为 failed，error code：`orphaned_after_process_restart` 或 `stale_running_reaped`。

不要只用 `threading.Lock`。它只能解决单进程并发，不能解决部署多 worker 或进程重启。也不要新增第二套 lock 实现，否则后续会同时维护 `_file_lock` 和 runner 专用 lock。

### A7. Sub-step 状态

内部子步骤建议固定枚举：

```text
validating_inputs
resolving_artifacts
building_draft
validating_compatibility
zipping_draft
registering_artifact
completed
failed
```

每次进入子步骤：

- 更新 `job.jianying_draft_substep`。
- `save_job(job)`。
- 追加 `JobEvent`：

```json
{
  "created_at": "<helper injected ISO timestamp>",
  "event_type": "status",
  "stage": "jianying_draft",
  "status": "running",
  "message": "正在生成剪映草稿",
  "payload": {
    "substep": "building_draft",
    "attempt_id": "...",
    "fingerprint": "...",
    "user_draft_root_mode": "absolute|relative"
  }
}
```

这里优先复用 `EVENT_TYPE_STATUS`，不要新增 `jianying.*` event type。只有当 admin dashboard 需要按 event_type 聚合时，再新增专用类型。

示例中的 `created_at` 应由本模块 helper 自动注入；如果直接构造 `JobEvent`，该字段不可省略。

### A8. 错误分类

`jianying_draft_error` 保持人类可读，但 `JobEvent.payload` 应增加机器可读字段：

```json
{
  "error_code": "missing_manifest",
  "error_class": "precondition",
  "substep": "resolving_artifacts",
  "recoverable": true
}
```

建议错误码：

| error_code | error_class | level | recoverable | 说明 |
| --- | --- | --- | --- | --- |
| `missing_manifest` | `precondition` | `error` | true | job 成功状态与产物缺失不一致，需要重跑或修复产物 |
| `missing_source_artifact` | `precondition` | `error` | true | 剪映输入素材缺失 |
| `invalid_user_draft_root` | `user_input` | `warn` | true | 用户路径输入不合法，通常由 400 返回处理 |
| `engine_unavailable` | `environment` | `error` | true | pyJianYingDraft / backend 不可用 |
| `draft_write_failed` | `writer` | `error` | true | 写草稿目录失败 |
| `compatibility_validation_failed` | `validation` | `error` | true | 兼容性校验失败 |
| `zip_write_failed` | `artifact` | `error` | true | zip 写入失败 |
| `artifact_register_failed` | `artifact` | `error` | true | artifact 注册失败 |
| `orphaned_after_process_restart` | `orphan_recovery` | `critical` | true | 进程重启留下 running 且无法自动收敛为 succeeded，需要 ops 关注 |
| `stale_running_reaped` | `orphan_recovery` | `critical` | true | stale running 被回收为 failed，需要 ops 关注；早期草案中的 `stale_lock_reaped` 不再推荐，因为本方案复用 `_file_lock`，不维护自定义 stale lock 文件 |
| `stale_running_recovered` | `orphan_recovery` | `warn` | false | stale running 找到 matching zip，自动收敛为 succeeded |
| `unexpected_exception` | `unknown` | `critical` | true | 未分类异常，需要 ops 介入 |

### A9. Status API 兼容

`GET /jobs/{id}/jianying-draft-status` 返回新增字段，但不破坏旧字段：

```json
{
  "status": "running",
  "started_at": "...",
  "completed_at": null,
  "error": null,
  "artifact_key": null,
  "draft_zip_path": null,
  "substep": "building_draft",
  "attempt_id": "...",
  "fingerprint": "..."
}
```

前端可先不消费 `substep`。如果后续要展示，只映射成自然中文：

- `validating_inputs` -> `正在检查任务产物`
- `resolving_artifacts` -> `正在整理素材`
- `building_draft` -> `正在写入剪映草稿`
- `validating_compatibility` -> `正在校验草稿兼容性`
- `zipping_draft` -> `正在打包草稿`

### A10. 测试计划

新增或扩展：

- `tests/test_jianying_draft_runner.py`

覆盖：

1. succeeded + same fingerprint + zip exists -> idempotent return。
2. succeeded + same fingerprint + zip missing -> regenerate。
3. succeeded + different `user_draft_root` -> regenerate。
4. running + valid lock -> return running，不新建线程。
5. running + stale lock + matching zip -> mark succeeded。
6. running + stale lock + no zip -> mark failed，error_code 为 orphan/stale。
7. concurrent trigger 只有一个能获得 lock。
8. substep 会写入 JobRecord 并追加 `JobEvent`。
9. backend exception 会释放 lock，并标记 failed。
10. `user_draft_root` invalid 仍返回原有 400 语义。

验收命令：

```powershell
pytest -q tests/test_jianying_draft_runner.py
pytest -q tests/test_phase2_download_backend.py tests/test_legacy_cleanup_guards.py
```

## 5. Phase B：LLM Attempt Audit

### B1. 目标

让每一次 LLM 尝试都有结构化记录，包括失败、重试、fallback、invalid output。

审计必须回答：

- 哪个任务触发了付费 LLM？
- 主模型是谁？
- 为什么 fallback？
- fallback 到了谁？
- 哪些 attempt 可能已经计费？
- 最终成功还是失败？
- 是否是 provider error、JSON parse error、validator failure、长度约束 failure？

### B2. 非目标

本阶段不做：

- 不记录完整 prompt / response 正文到 JobEvent 或 Gateway DB。
- 不做完整成本结算。
- 不改价格 catalog。
- 不重构 `llm_registry.py`。
- 不把所有历史 LLM 入口一次性改完。

### B3. 记录位置

主记录位置：

- `UsageMeter.record_llm(...)`
- `{project_dir}/metering/usage_events.jsonl`

辅助摘要位置：

- 对 fallback decision 追加 `JobEvent`，使用 `EVENT_TYPE_LOG` 或 `EVENT_TYPE_STATUS`。
- JobEvent 只放摘要，不放 prompt。

原则：

- 详细 attempt 明细在 UsageMeter。
- 用户/管理员可读的阶段事件在 JobEvent。
- Gateway `Job.metering_snapshot` 仍承载聚合结果。
- `record_llm` 及其 helper 必须保持 best-effort：任何审计写入失败只能 log/print warning，不能让已经成功的 LLM 调用或翻译流程失败。付费调用已经发生时，不能因为 sidecar 写盘失败把主业务结果回滚。

### B4. 第一阶段入口范围

先改最集中的入口：

- `src/services/gemini/translator.py::_call_task_with_fallback`

第一阶段必须覆盖这个函数里的两个分支：

- llm_registry 新路径：`model_name -> _call_by_model(...) -> except (TranslationError, LLMProviderError)`。
- legacy LLMRouter 路径：`route alias -> generate_via_alias(...) / _call_gemini_with_retry(...) -> except (TranslationError, LLMProviderError)`。

覆盖任务：

- `s3_translate`
- `s5_rewrite`
- `s5_rewrite_strict`
- `s5_short_content_compact`
- `s2_infer`
- `s2_review`
- `content_compliance`

第二阶段再扩：

- `src/services/transcript_reviewer.py`
- `src/services/assemblyai/semantic_segmenter.py`
- `src/services/assemblyai/speaker_corrector.py`
- `src/services/gemini/transcriber.py` legacy guard

不要第一轮全仓铺开。

与 LLMRouter deprecation 的关系：

- `_call_task_with_fallback` legacy path 当前还有 `runtime_logs/llm-router-legacy.log` 观察期，用于判断 2026-05-16 后能否清理旧路由。
- P1 的 failure attempt metering 应与这个观察共享事实来源：legacy path 命中时继续保留现有 legacy log，同时补 `UsageMeter.record_llm(...)` 的 success/failure attempt。
- 不要形成两套互相矛盾的 audit。后续 deprecation 判断应优先看 UsageMeter 聚合，legacy log 只作为过渡证据。

### B5. Attempt 事件字段

`UsageMeter.record_llm(...)` 当前已经有大部分字段。建议先不改接口，先规范调用：

```python
meter.record_llm(
    task=task,
    phase=phase,
    provider=provider,
    model=logical_model,
    model_id=api_model_id,
    input_text=prompt,
    output_text=response_text_or_empty,
    attempt_label=attempt_label,
    success=success,
    error=error_summary,
)
```

调用要求：

- 对 `record_llm(...)` 的调用必须包在 try/except 或走现有 `_record_llm_usage` 这类 best-effort wrapper。
- audit failure 只写 warning，不抛出到 LLM 主路径。
- 完整 prompt 可以传给 `UsageMeter` 用于长度和 token 估算，但不应写入 JobEvent 或 Gateway DB；如果后续调整 UsageMeter 事件，也不得落完整正文。

需要新增时再加字段：

```json
{
  "kind": "llm",
  "task": "s3_translate",
  "phase": "translation",
  "provider": "gemini",
  "model": "gemini",
  "model_id": "gemini-2.5-flash-lite",
  "attempt_label": "primary|retry_1|fallback_1",
  "success": false,
  "error": "json_parse_failed: ...",
  "error_class": "invalid_output",
  "error_code": "json_parse_failed",
  "duration_ms": 4200,
  "fallback_from": "gemini_pro",
  "fallback_to": "deepseek",
  "provider_response_received": true,
  "prompt_hash": "sha256:..."
}
```

实现顺序建议：

1. 先用现有字段落 `success/error/attempt_label`。
2. 再最小扩 `UsageMeter.record_llm` 支持 `extra: dict[str, object] | None`，把 `error_class/duration_ms/prompt_hash` 放进去。
3. 更新 summary 聚合时忽略未知字段，避免破坏旧事件。

`prompt_hash` 留存策略：

- `prompt_hash` 只用于同一 job、同一 task 的多 attempt 关联和去重诊断。
- 不把 `prompt_hash` 作为跨任务、跨用户的长期画像键。
- 不在普通用户可见接口暴露 `prompt_hash`。

### B6. Error 分类

新增纯函数：

```python
def classify_llm_error(exc: Exception) -> tuple[str, str]:
    ...
```

建议分类：

- `provider_error`
  - HTTP error、timeout、SDK exception、quota/rate limit。
- `auth_error`
  - API key missing / invalid credential。
- `invalid_output`
  - JSON parse failure、schema mismatch、validator failure。
- `length_constraint_failed`
  - 输出字数超出 min/max，触发 retry/fallback。
- `configuration_error`
  - unknown model、provider missing、route empty。
- `cancelled`
  - 后续如果支持取消。
- `unknown_error`

记录策略：

- provider 调用抛异常：`provider_response_received=false`。
- provider 返回了文本但 validator 失败：`provider_response_received=true`，`success=false`，这类 attempt 很可能已计费。
- 同模型 retry：`attempt_label=retry_1` / `retry_2`。
- 跨模型 fallback：`attempt_label=fallback_1` / `fallback_2`。

### B7. Fallback 决策 JobEvent

在 fallback 发生时追加轻量 JobEvent：

```json
{
  "created_at": "<helper injected ISO timestamp>",
  "event_type": "log",
  "stage": "llm",
  "level": "warn",
  "message": "LLM 调用失败，已切换备用模型",
  "payload": {
    "task": "s3_translate",
    "from_model": "gemini_pro",
    "to_model": "deepseek",
    "error_class": "provider_error",
    "attempt_label": "fallback_1"
  }
}
```

这需要 translator 拿到 `JobStore` 才能直接写 JobEvent。第一轮可以不强行打通：

- 先保证 `UsageMeter` 明细完整。
- `process.py` 在写回 metering summary 后，可以根据 summary 生成 job-level fallback 摘要。
- 如果要实时 admin 可见，再给 translator 注入一个 `event_recorder` 回调。

不要为了这个目标新增全局 event bus。

示例中的 `created_at` 应由 JobEvent helper 注入；如果直接构造 `JobEvent`，该字段不可省略。

### B8. 付费 API 硬约束检查

每个 fallback 记录必须包含：

- `fallback_reason`
- `fallback_policy_source`
  - `admin_prompt_models`
  - `llm_registry_defaults`
  - `legacy_router`
- `user_or_admin_configured`
  - true / false

目的：

- 证明 fallback 不是异常分支里偷偷临时引入的新付费 API。
- 后续可以审计“这个付费 provider 是用户/管理员配置允许的，还是代码默认兜底”。

### B9. 测试计划

新增或扩展：

- `tests/test_gemini_translator.py`
- `tests/test_usage_meter.py`

覆盖：

1. primary success 记录一条 success attempt。
2. primary provider error + fallback success 记录两条 attempt。
3. primary 返回 invalid JSON + fallback success，primary 记录 `success=false` 且 `provider_response_received=true`。
4. 所有 attempt 事件不包含完整 prompt / response。
5. `attempt_label` 稳定。
6. error 字段截断，不污染日志。
7. legacy LLMRouter path 命中时也记录 success/failure attempt，并继续满足 LLMRouter deprecation 观察期需要。
8. `record_llm` 写入失败不会让 `_call_task_with_fallback` 的成功响应变失败。

验收命令：

```powershell
pytest -q tests/test_usage_meter.py tests/test_gemini_translator.py
pytest -q tests/test_llm_router.py tests/test_process_pipeline.py
```

## 6. Phase C：JobEvent 使用准则

### C1. 不新增第二套 sink

所有 job 生命周期事件继续走：

```python
store.append_event(job_id, JobEvent(...))
```

Gateway download 路由事件继续走：

```python
gateway.storage.event_log.emit_download_event(...)
```

不新增：

- `WorkflowEventSink`
- `PipelineEventBus`
- 独立 workflow JSONL
- 独立 gateway event schema

### C2. Event type 扩展规则

优先使用已有类型：

- `status`：状态变化、substep、running/succeeded/failed。
- `log`：诊断事件、fallback、warning、ops intervention。
- `download.*`：只给下载路由决策。

只有满足以下条件才新增 event type：

- 需要在 dashboard 上按 event_type 聚合。
- 语义稳定，不会随着内部阶段命名频繁变化。
- Job API 和 Gateway 是否都会写入已经明确。

如果新增 Gateway 写入类型：

- 改 `src/services/jobs/events.py`。
- 改 `gateway/storage/event_log.py` 对应 allow-list / writer。
- 加或改同步测试。
- 继续遵守 Gateway 不能 import `services.jobs.events` 的边界。

### C3. 推荐 payload 规范

所有 `JobEvent` 记录必须包含 `created_at`。建议每个模块使用 `_utc_now_iso()` / `_emit_event(...)` helper 注入，不要在调用点手写重复时间逻辑。

公共字段：

```json
{
  "created_at": "<helper injected ISO timestamp>",
  "attempt_id": "...",
  "substep": "...",
  "error_code": "...",
  "error_class": "...",
  "recoverable": true,
  "duration_ms": 1234
}
```

LLM payload：

```json
{
  "created_at": "<helper injected ISO timestamp>",
  "task": "s3_translate",
  "prompt_key": "translate",
  "provider": "gemini",
  "model": "gemini",
  "model_id": "gemini-2.5-flash-lite",
  "attempt_label": "fallback_1",
  "prompt_hash": "sha256:..."
}
```

剪映 payload：

```json
{
  "created_at": "<helper injected ISO timestamp>",
  "fingerprint": "...",
  "substep": "zipping_draft",
  "artifact_key": "editor.jianying_draft_zip",
  "user_draft_root_mode": "absolute"
}
```

禁止：

- 完整 prompt。
- 完整 response。
- API key。
- 本地敏感路径的未脱敏副本。
- 大段 traceback。

`prompt_hash` 是允许字段，但只用于同 job / 同 task 多 attempt 关联；不要把它作为跨任务长期分析维度，也不要暴露给普通用户接口。

## 7. Deferred：Registry 收敛

### 7.1 当前不做大改

`llm_registry.py` 当前已经承担：

- logical model -> API model id。
- provider。
- supports audio。
- auth。
- cost rank。
- 默认 prompt model。
- fallback candidates。

不要一次性补齐以下字段：

- `context_window`
- `supports_json_mode`
- `max_output_tokens`
- `fallback_group`
- `stability`
- `deprecation_at`

### 7.2 触发条件

满足任一条件再扩：

- 新模型上线导致 max token / JSON mode / audio capability 真的分歧。
- 同一 provider 下不同模型 fallback 规则不再能用 `cost_rank` 表达。
- admin UI 需要展示更细能力。
- audit 数据显示大量失败来自模型能力不匹配。

### 7.3 最小扩展顺序

如果必须扩，顺序为：

1. `supports_json_mode`
2. `max_output_tokens`
3. `context_window`
4. `fallback_group`
5. `lifecycle_status`

不要先做 prompt version tag。只有进入 A/B 对比或灰度评估时，prompt version 才有实际价值。

## 8. Deferred：Checkpoint / Resume

### 8.1 当前不做全 pipeline checkpoint

原因：

- 需要触及每个 stage 输入 hash、manifest schema、恢复入口。
- 改动面大，容易做出没人使用的精致系统。
- 现阶段还没有足够数据说明失败主要集中在哪里。

### 8.2 数据驱动触发条件

LLM attempt audit 和 runner event 落地后，先观察失败分布：

- 如果 80% 失败集中在 TTS 单段：优先做 S5 段级重试 / 局部 resynth。
- 如果 80% 失败集中在 LLM invalid output：优先调 prompt / validator / fallback。
- 如果失败集中在剪映 zip：继续加固 runner / artifact validation。
- 如果失败均匀分布，才考虑全 pipeline checkpoint。

### 8.3 可先做的小 checkpoint

允许局部做：

- 剪映 draft fingerprint。
- LLM prompt hash + model policy hash。
- TTS segment-level output existence check。
- Subtitle cue quality report gate。

不做统一大框架。

## 9. 建议实施顺序

### Step 1：Runner fingerprint + lock

文件：

- `src/services/jobs/models.py`
- `src/services/jobs/jianying_draft_runner.py`
- `tests/test_jianying_draft_runner.py`

完成标准：

- fingerprint 来自关键 artifact 内容 hash 或 normalized manifest，不使用完整 manifest 文件 hash。
- 同 fingerprint 可复用。
- 并发触发不会双跑。
- 复用 `src/services/_file_lock.py::file_lock`，不新增 runner 专用 lock 实现。
- stale/orphan running 可回收，阈值与 `STALE_THRESHOLD_SECONDS=1800` 对齐。

### Step 2：Runner sub-step JobEvent

文件：

- `src/services/jobs/events.py`（通常无需新增 type）
- `src/services/jobs/jianying_draft_runner.py`
- `src/services/jobs/store.py`（如需 helper）
- `tests/test_jianying_draft_runner.py`

完成标准：

- 每个关键 substep 有 `status` event。
- event 写失败不影响主流程。
- status API 可返回 substep。

### Step 3：LLM failure attempt metering

文件：

- `src/services/usage_meter.py`
- `src/services/gemini/translator.py`
- `tests/test_usage_meter.py`
- `tests/test_gemini_translator.py`

完成标准：

- provider error / invalid output / fallback 都有 usage event。
- llm_registry 新路径与 legacy LLMRouter 分支都覆盖 failure attempt。
- `record_llm` 写入失败不影响主路径。
- 不记录完整 prompt / response。
- summary 不被失败事件破坏。

### Step 4：LLM fallback summary event

文件：

- `src/pipeline/process.py` 或 translator event callback 注入点。
- `src/services/jobs/service.py` / runner wiring，视现有依赖方向决定。

完成标准：

- JobEvent 中能看到 fallback 摘要。
- 不新增第二套 event sink。
- 不让 event 写入失败影响 pipeline。

### Step 5：根据数据决定是否扩 registry / checkpoint

输出：

- 统计最近 N 个 failed job 的失败分布。
- 给出是否进入 registry/checkpoint 下一阶段的判断。

## 10. 验收标准

### 10.1 功能验收

- Studio succeeded job 触发剪映草稿生成，成功后再次触发直接复用 zip。
- 修改 `user_draft_root` 后触发重建。
- 生成中重复点击不会生成两个后台线程或两个 zip。
- 模拟进程重启/orphan running 后，reaper 能收敛到 succeeded 或 failed。
- LLM fallback 发生后，usage_events 里有 primary failure 和 fallback success 两条记录。
- invalid JSON / validator failure 会记录为 failed attempt，而不是丢失。

### 10.2 架构验收

- 没有引入 Google ADK。
- 没有新增 workflow event sink。
- Gateway 仍不 import `services.jobs.events`。
- 默认本地测试不调用真实外部 API。
- `main.py` 和 `pytest` 在 clean local 环境仍可跑。
- 前端不成为模型/价格/entitlement 真源。

### 10.3 安全与隐私验收

- JobEvent 不包含完整 prompt、response、API key。
- UsageMeter 只保留长度、token 估算、provider/model、错误摘要。
- 错误摘要有长度限制。
- `prompt_hash` 只用于同任务 attempt 关联，不作为跨用户/跨任务画像键。
- 本地路径按现有 logs redactor 策略处理，不在 admin 普通视图泄漏敏感路径。

## 11. 风险与规避

### 风险 1：Runner lock 与 JobRecord 状态分叉

规避：

- 复用 `services._file_lock.file_lock`，不新增 `os.O_EXCL` 自定义 lock。
- 所有 read-modify-write 状态切换都在 file lock 临界区内完成。
- `attempt_id` / `pid` / `fingerprint` 写入 JobRecord 和 JobEvent，不写自定义 lock 文件格式。
- reaper 在同一 lock 下处理 stale `running`，阈值与 `JianyingDraftRunner.STALE_THRESHOLD_SECONDS` 对齐。
- 如果需要非阻塞 lock 探测，只扩展 `_file_lock.py`，不新建第二套工具。

### 风险 2：UsageMeter 事件变多导致 summary 变慢

规避：

- 第一阶段只覆盖 translator fallback path。
- summary 聚合按 `kind/task/provider/model/success` 做简单聚合。
- 大量事件再考虑按阶段拆文件，不提前优化。

### 风险 3：事件类型膨胀

规避：

- 默认只用 `status/log`。
- 新 event type 必须说明 dashboard 聚合需求。
- Gateway 写入类型必须同步测试。

### 风险 4：付费 fallback 被“审计合理化”

规避：

- audit 只记录事实，不授权新 fallback。
- 新增付费 provider fallback 必须来自 admin/user 明确配置。
- 测试中用 fake provider，默认路径不打真实 API。

## 12. 不接 ADK 的理由

ADK 2.0 的 graph workflow 思路有参考价值，但当前不适合作为主路径：

- ADK 2.0 仍是 pre-GA / Beta。
- 本项目核心是确定性 pipeline，不是让 agent 动态决定流程。
- 现有 `JobEvent`、`UsageMeter`、`JobRecord`、`JianyingDraftRunner` 已经提供足够落点。
- 引入 ADK 会增加 session/storage/runtime 复杂度，且不直接修复 orphan runner 或 LLM attempt audit。

可借鉴的只有方法论：

- 显式节点。
- 显式状态。
- 显式事件。
- 显式 fallback。

实现仍留在本项目自己的小模块里。

## 13. 最小可交付切片

如果只做一个最小闭环，建议顺序如下：

1. `JianyingDraftRunner` 加 fingerprint。
2. `JianyingDraftRunner` 复用 `services._file_lock.file_lock` 做跨进程互斥。
3. `reap_stale()` 从单纯 failed 改为 orphan recovery。
4. runner substep 写 `EVENT_TYPE_STATUS`。
5. translator `_call_task_with_fallback` 对失败 attempt 调 `UsageMeter.record_llm(success=False, ...)`，覆盖 llm_registry 新路径和 legacy LLMRouter 两个 except 分支。
6. tests 覆盖 runner 幂等/并发/stale 和 LLM fallback audit。

这个切片不需要改前端、不需要改 Gateway DB、不需要新依赖，也不需要接真实外部 API。
