# APF3a 匿名 Free/Express 预览准入 contract（local/mock 边界）

**状态：** PROPOSED — contract shell 已落地，待 APF3b/APF3c 真实预览 pipeline 接入
**日期：** 2026-06-04
**Phase envelope：** `APF3a`（`docs/ai-workgroup/shared/phase-envelope.current.json`）
**业务方案锚点：** `docs/plans/2026-06-01-anonymous-preview-funnel-ux-plan.md` §10、§12、§16
**前置 contract：** `docs/plans/2026-06-02-apf2-anonymous-intake-contract.md`、`docs/plans/2026-06-02-apf2c-backend-adapter-boundary.md`

---

## 1. 目标

为匿名 Free / Express / Smart / Studio 四档建立**纯本地** preview 准入 contract，输出一个不可下载、必带水印、不暴露 provider 字段的状态记录。本阶段**不**实现真实预览生成、**不**接 provider、**不**接 Gateway、**不**改 frontend、**不**触碰任何付费 API。

## 2. 边界

落点：`src/services/anonymous_preview_admission.py`（新文件，沿用 `anonymous_preview_*.py` 命名）。

允许导入：仅标准库（`dataclasses` / `enum` / `math` / `typing`）。

明确禁止：

- 不 import `gateway`、`frontend-next`、`src.pipeline`、`src.modules`；
- 不 import `src.services.tts*`、`src.services.voice_clone`、`src.services.content_compliance`、`src.services.usage_meter`、`src.services.jobs.*`、`src.services.assemblyai`、`src.services.gemini`、`src.services.llm*`；
- 不 import `requests` / `urllib*` / `httpx` / `aiohttp` / `boto3` / `subprocess`；
- 不开文件，不发网络请求，不起子进程，不查 DB；
- 不调用任何 ASR / LLM / TTS / clone / 预览渲染 / 对象存储 / 支付 / 点数 provider；
- 不读 `.env`，不读任何生产 secret；
- 不注册 endpoint，不改 docker / deploy / migrations。

测试模块：`tests/test_apf3a_anonymous_preview_contract.py`。AST 守卫会扫上述禁忌 import 和禁忌 call。

## 3. 决策表

| `mode` | 默认 `decision` | `preview_duration_seconds` | `voice_strategy` | `next_step_hint` |
| --- | --- | --- | --- | --- |
| `free` | `admitted` | `min(source, 180)` | `preset_only` | `None` |
| `express` | `admitted` | `min(source, 180)` | `preset_only`（默认）/ `express_temporary_clone_gate`（flag 为 `True` 时） | `None` |
| `smart` | `login_required` | `0.0` | `preset_only` | `login_required` |
| `studio` | `not_anonymous_funnel` | `0.0` | `preset_only` | `studio_requires_login_and_entitlement` |
| 非法 mode | `rejected` | `0.0` | `preset_only` | `fix_input_and_retry` |
| 非法 duration / 缺 config | `failed` | `0.0` | `preset_only` | `retry_or_contact_support` |

## 4. Artifact policy（所有 mode 默认锁死）

`AnonymousPreviewArtifactPolicy` 字段默认值即最严格策略，调用方拿到任何 admission（包括拒绝态）都会看到：

- `watermark_required=True`
- `allow_download_url=False`
- `allow_subtitle_export=False`
- `allow_jianying_draft_export=False`
- `allow_payment_fields=False`
- `allow_provider_voice_id=False`
- `allow_clone_artifact=False`

为对齐 APF3a 任务卡显式 contract primitives，新增四个 **required marker**（r1，2026-06-04）：

- `stream_only_required=True`——下游必须 stream-only 投递，不得提供可下载文件；
- `allow_editable_assets=False`——禁止暴露字幕导出、剪映草稿、原始 TTS 片段等可编辑资产；
- `artifact_ttl_required=True`——下游对象存储/缓存必须设置 TTL，APF3a 不固化具体秒数；
- `low_priority_required=True`——下游队列必须按低优先级排程，APF3a 不固化具体 priority 数值。

> **边界声明**：APF3a 仍是 local/mock contract shell，故意只暴露布尔 required marker，**不**承诺 TTL 秒数、queue priority 数值、storage bucket 名等任何 storage / queue 具体数。具体数值留给后续 phase（APF3b/APF3c）在真正接入存储与队列时再定。
>
> `AnonymousPreviewArtifactPolicy` dataclass 的字段口径在 r1 之后约束如下（避免与 `allow_download_url=False` / `allow_provider_voice_id=False` / `allow_payment_fields=False` 这类合法 deny marker 自相矛盾）：
>
> - **允许**新增 `allow_*` 负向布尔 deny marker（例如 `allow_download_url=False`），以及 `*_required` 必需标记（例如 `stream_only_required=True`）；这些是策略表态，本身不承载真实值；
> - **禁止**新增任何承载真实值的 URL / endpoint / provider / payment / credit / voice_id / token 字段，例如 `preview_url` / `download_url_value` / `provider_voice_id` / `provider_voice_id_value` / `payment_token` / `pricing_quote` / `credit_reservation_id` / `clone_reservation_id` / `token` / `endpoint` / `preview_artifact_key`；这些含真实承载值的字段属于后续接入存储 / 队列 / 计价的 phase；
> - 回归守卫双层防护：(1) 显式 allowlist 限定当前合法 marker 字段集；(2) 精确 denylist 兜底常见承载值命名。见 `tests/test_apf3a_anonymous_preview_contract.py::test_artifact_policy_dataclass_has_no_forbidden_url_or_provider_fields`。

`FORBIDDEN_ADMISSION_FIELDS` 集合显式列出 deny list（`preview_url` / `download_url` / `clone_provider_voice_id` / `payment_token` / `pricing_quote` 等），AST 守卫与 dataclass 字段扫描双重保护。

## 5. Clone gate 边界

`anonymous_express_cosyvoice_clone_enabled` 在 `AnonymousPreviewAdmissionConfig` 中**显式默认 `False`**。即便外部把它显式置 `True`：

- `voice_strategy` 会切换为 `EXPRESS_TEMPORARY_CLONE_GATE`，**仅为边界标记**；
- 本模块**不**实现任何 provider 调用，下游若要落实克隆必须先调 `raise_clone_provider_boundary(mode)`，该函数**永远**抛 `NotImplementedError`；
- 任何 truthy 非 `True` 值（如 `"true"` 字符串）**不被接受**，仍走 `preset_only` 路径。

这条边界与 `CLAUDE.md` "付费 API 不能自动调用" 硬约束、`anonymous_express_cosyvoice_clone_enabled` admin 主开关一致：没有显式 `True` flag、没有 reservation、没有 worker runtime gate，就不允许进入克隆。

## 6. Fail-closed 行为

调用方传入以下任一情况，函数返回 `decision=FAILED` 或 `decision=REJECTED`，绝不静默 fallback：

- `config is None`；
- `config.max_preview_duration_seconds` 非正数 / `nan` / `inf`；
- `source_duration_seconds` 为负、`nan`、`inf`、非数值；
- `mode` 不在 `AnonymousPreviewMode` 枚举内。

`AdmissionRejected` 仅作为内部信号，调用方永远拿到的是状态记录，不会有未捕获异常逸出。

### 6.1 未知 mode 的 reason redaction（r1，2026-06-04）

`_coerce_mode()` 处理非法 `mode`（任意非字符串对象 / 字符串但不在枚举值内）时，**绝不**把原值通过 `repr(mode)` / `str(mode)` / `f"{mode!r}"` 回写到 `admission.reason`。原因：

- `admission.reason` 可能被 status API、日志、面向用户的错误提示透出；
- 攻击者可以通过 mode 参数夹带 `Bearer ...`、`sk_live_...`、`path=/tmp/...`、HTML payload 等敏感片段；
- 把 raw mode 回显等于把这些片段散到日志面板 / 截图 / 客服工单。

修复方式：所有未知 mode 都返回稳定常量 `UNKNOWN_MODE_REASON = "unknown anonymous preview mode (fail closed)"`，**不携带任何输入**。回归守卫：

- `test_unknown_string_mode_reason_does_not_echo_raw_input`——参数化扫 `Bearer`、`sk_live_*`、`path=/tmp/...`、HTML payload 等输入；
- `test_unknown_non_string_mode_reason_does_not_echo_repr`——构造 `__repr__` 里带 secret 的对象，断言 `admission.reason` 不含 `repr` 片段；
- `test_admission_source_has_no_mode_repr_format_strings`——AST 扫源码，禁止 `f"...{mode!r}..."` / `repr(mode)` / `str(mode)`，防止未来回滚。

## 7. 状态记录契约

`AnonymousPreviewAdmission` 是 frozen dataclass，字段固定为：

```
mode, decision, preview_duration_seconds, voice_strategy,
artifact_policy, reason, next_step_hint
```

不存在 `preview_url`、`download_url`、`clone_voice_id`、`payment_token`、`pricing_quote` 任何一个字段——dataclass 类定义本身就是 source of truth，测试再用 `FORBIDDEN_ADMISSION_FIELDS` 二次断言。

## 8. 与 APF2 / APF2c 的关系

- APF2 (`anonymous_preview_intake`) 决定**能否进入匿名漏斗**（source type / upload gate / 合规 / 限频）；
- APF2c (`anonymous_preview_backend_adapter`) 把 intake 结果包成 `PreviewRecord`；
- **APF3a (`anonymous_preview_admission`) 决定 `READY_FOR_MODE` 之后某一个 mode 是否能进 3 分钟预览，以及该预览的 artifact policy 与 voice 策略**。

APF3a 不读取 `PreviewRecord`，只接受调用方解析出的 `mode + source_duration_seconds`，保持模块独立、可单独单元化。

## 9. 未实现 / 留给后续 phase

- APF3a 不生成任何预览媒体；
- 不调度任务、不落盘、不写日志；
- Express 临时克隆 provider 接入 / reservation / runtime gate 留给后续 phase；
- 匿名 quota 1 次/天的真实计数仍走 `anonymous_preview_rate_limit` counter store；本模块只暴露默认值常量。

## 10. 测试覆盖

`tests/test_apf3a_anonymous_preview_contract.py` 覆盖：

1. 默认值（180 s 上限、Free/Express 1 次/天、clone flag 默认关、artifact 默认全锁）；
2. Free / Express 接受 + 180 s 封顶 + 源时长更短时使用源时长 + 边界值；
3. Smart → `login_required`；Studio → `not_anonymous_funnel`；
4. 所有 mode 的 artifact policy 都锁死、dataclass 不含 forbidden 字段、admission frozen 不可变；
5. Express clone gate：默认关、`"true"` 字符串不开门、显式 `True` 仅给 boundary marker、`raise_clone_provider_boundary` 必抛 `NotImplementedError`；
6. fail-closed 分支：缺 config、`max_preview_duration_seconds` 非法、`source_duration_seconds` 非法 / 非数值、未知 mode；
7. AST 守卫：admission 模块不导入 forbidden 模块、不调用 forbidden API、不暴露 forbidden 命名；
8. `AdmissionRejected` 异常携带 decision + reason。
