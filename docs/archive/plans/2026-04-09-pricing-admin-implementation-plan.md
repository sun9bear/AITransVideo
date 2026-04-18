# 定价管理实施计划

> **给执行型 agent 的要求：** 必须使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans` 按任务逐步实施本计划。所有步骤使用复选框 `- [ ]` 跟踪。

**目标：** 新增一个数据库驱动、支持版本化发布的商业定价真值系统，以及一个独立的后台定价管理页面；同时避免让同步运行时模块直接依赖 async 数据库查询。

**架构：** 将商业真值存入新的 `pricing_config_versions` 表，使用版本化 JSON payload 管理。发布时把当前 active 版本同步写入运行时快照文件，并做进程内缓存，这样 [plan_catalog.py](/D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/plan_catalog.py)、[credits_service.py](/D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/credits_service.py)、[voice_selection_api.py](/D:/Claude/AIVideoTrans_Codex_web_mvp/gateway/voice_selection_api.py) 这类同步模块可以直接消费当前真值，而不需要直接查 async DB。后台通过 admin-only draft / publish / history API 和独立的 `/admin/pricing` 页面来编辑套餐、Trial、点数、Top-up、成本校准参数。

**技术栈：** FastAPI、SQLAlchemy、Alembic、Pydantic、PostgreSQL JSONB、Next.js App Router、现有 admin shell、pytest

---

## 文件规划

**新增**
- `gateway/pricing_schema.py`
  - 定义定价 payload 的 Pydantic 模型：`plans`、`trial`、`credits`、`topup`、`cost_model`
  - 包含校验逻辑和 `build_default_pricing_payload()`
- `gateway/pricing_runtime.py`
  - 运行时快照文件路径、缓存读写、失效刷新
  - 提供同步 accessor 给运行时代码使用
- `gateway/pricing_admin.py`
  - admin-only 的 draft / publish / history API
  - 包含获取最新 draft / active 版本 / publish 事务的 DB helper
- `gateway/alembic/versions/011_add_pricing_config_versions.py`
  - 新增 `pricing_config_versions` 表的 migration
- `frontend-next/src/app/(app)/admin/pricing/page.tsx`
  - 独立的后台定价管理页面
- `frontend-next/src/lib/admin/pricing.ts`
  - 前端访问 pricing admin API 的 typed fetch helper
- `tests/test_pricing_schema.py`
  - payload 校验和默认值测试
- `tests/test_pricing_runtime.py`
  - 快照文件读写、缓存失效、fallback 测试
- `tests/test_pricing_admin.py`
  - admin pricing API 的 draft / publish / history 测试

**修改**
- `gateway/models.py`
  - 新增 `PricingConfigVersion`
- `gateway/main.py`
  - 挂载 pricing router
  - 启动时 seed runtime pricing snapshot
- `gateway/plan_catalog.py`
  - 从 runtime pricing payload 派生 plan / trial 真值
- `gateway/credits_service.py`
  - 从 runtime pricing payload 派生 debit rate、grant amount、bucket priority
- `gateway/billing.py`
  - 去掉 request-time 对 import-time 冻结价格集的依赖
- `gateway/job_intercept.py`
  - 去掉对 import-time 冻结 `PLAN_CATALOG` 的匿名 fallback 依赖
- `gateway/voice_selection_api.py`
  - 从 runtime pricing truth 读取 clone cost 和展示价格
- `gateway/admin_settings.py`
  - 将 `voice_clone_cost_credits` 标记为 deprecated，保留兼容但不再作为真值来源
- `src/pipeline/process.py`
  - 让 `voice_selection_review` payload 里的 `clone_cost_credits` 改为读取 runtime pricing
- `frontend-next/src/components/app-shell.tsx`
  - 增加 `/admin/pricing` 的后台导航入口
- `frontend-next/src/app/(app)/admin/settings/page.tsx`
  - 隐藏旧的 clone cost 入口，或显示“已迁移到定价管理”提示
- `tests/test_plan_catalog.py`
  - 改成断言 runtime 派生的 plan / trial 真值
- `tests/test_billing.py`
  - 改成断言下单逻辑使用当前 runtime 价格
- `tests/test_gateway_job_policy.py`
  - 断言 runtime 派生的 plan gate 仍然正确生效
- `tests/test_gateway_create_job.py`
  - 断言 create-job 策略读取 runtime gate 值
- `tests/test_voice_selection_pricing.py`
  - 断言 runtime clone cost 能透传到 voice selection pricing 接口
- `tests/test_voice_selection_payload.py`
  - 断言 `process.py` 的 payload builder 使用 runtime clone cost

**V1 明确不做**
- 不实现 Top-up 购买闭环
- 不让 `cost_model` 直接驱动线上 live debit
- 不把商业定价真值继续塞回 `admin_settings.json`

---

## 运行时 payload 约定

新的 runtime payload 结构建议固定为：

```json
{
  "version": 1,
  "catalog_frozen": true,
  "plans": {
    "free": { "display_name": "Free", "free_quota_total": 5, "max_duration_minutes": 10, "max_concurrent_jobs": 1, "allowed_service_modes": ["express"], "self_serve": false },
    "plus": { "display_name": "Plus", "price_cny_fen": { "monthly": 9900, "quarterly": 26900, "annual": 99900 }, "max_duration_minutes": 45, "max_concurrent_jobs": 3, "allowed_service_modes": ["express", "studio"], "self_serve": true, "monthly_grant_credits": 3500 },
    "pro": { "display_name": "Pro", "price_cny_fen": { "monthly": 29900, "quarterly": 79900, "annual": 299900 }, "max_duration_minutes": 180, "max_concurrent_jobs": 5, "allowed_service_modes": ["express", "studio"], "self_serve": true, "monthly_grant_credits": 12000 }
  },
  "trial": {
    "frozen": true,
    "days": 7,
    "source_minutes": 20,
    "includes_studio": true,
    "phone_required": true,
    "auto_charge": false,
    "fallback_plan": "free",
    "grant_credits": 300
  },
  "credits": {
    "free_grant_credits": 500,
    "debit_rates": {
      "express.standard": 10,
      "studio.standard": 15,
      "studio.high": 30,
      "studio.flagship": 50
    },
    "bucket_priority": {
      "express": ["free", "subscription", "topup", "trial"],
      "studio": ["trial", "subscription", "topup", "free"]
    },
    "voice_clone_cost_credits": 500
  },
  "topup": {
    "enabled": false,
    "packages": [
      { "code": "topup_1000", "credits": 1000, "price_cny_fen": 3900, "active": true, "sort_order": 10 },
      { "code": "topup_3000", "credits": 3000, "price_cny_fen": 9900, "active": true, "sort_order": 20 }
    ]
  },
  "cost_model": {
    "point_cost_rmb": 0.015,
    "point_price_rmb": 0.03,
    "target_gross_margin": 0.5,
    "k_cn_chars_per_src_min": 250,
    "fx_usd_cny": 7.0,
    "translate_cost_rmb_per_src_min": 0.03,
    "s2_review_cost_rmb_per_src_min": 0.02,
    "rewrite_cost_rmb_per_src_min": 0.02,
    "server_cost_rmb_per_src_min": 0.03
  }
}
```

这里的原则是：
- DB 是最终真值源
- runtime 文件是当前 active 真值的发布缓存

---

## 与 V3 Pilot 的关系和约束

本方案与当前 V3 shadow pilot 不冲突，但必须遵守一个明确时序约束：

- V3 pilot 校准评审完成前，**不得 publish `credits.debit_rates` 变更**
- 在 pilot 期间，允许先上线：
  - pricing version 表
  - draft / publish 机制
  - 后台定价管理页面
  - 非 live debit 字段的迁移，例如 `voice_clone_cost_credits`
- 在 pilot 期间，如果确需修改冻结扣点参数，必须视为专项运营变更，并在变更 note 中注明影响范围

原因：
- 当前 shadow credits 仍在采数
- pilot 期间 publish 新 debit rate 会导致同一批观测数据前后口径不一致
- 这会直接影响 K-value、毛利校准、Plus/Pro 附赠点数判断

---

### 任务 1：新增 Pricing Schema 和默认 payload 构建器

**涉及文件：**
- 新增：`gateway/pricing_schema.py`
- 测试：`tests/test_pricing_schema.py`

- [ ] **步骤 1：先写失败测试**

```python
def test_default_payload_matches_current_frozen_values():
    payload = build_default_pricing_payload()
    assert payload.plans.plus.price_cny_fen.monthly == 9900
    assert payload.trial.grant_credits == 300
    assert payload.credits.debit_rates["express.standard"] == 10
    assert payload.credits.voice_clone_cost_credits == 500


def test_trial_fallback_plan_must_exist():
    bad = build_default_pricing_payload().model_dump()
    bad["trial"]["fallback_plan"] = "enterprise"
    with pytest.raises(ValidationError):
        PricingPayload.model_validate(bad)
```

- [ ] **步骤 2：运行测试，确认它先失败**

运行：
`C:\Users\Administrator\.local\bin\python.cmd -m pytest tests/test_pricing_schema.py -q`

预期：
缺模块或缺符号，测试失败

- [ ] **步骤 3：实现最小 schema**

```python
class PricingPayload(BaseModel):
    version: int = 1
    catalog_frozen: bool = True
    plans: dict[str, PlanConfig]
    trial: TrialConfig
    credits: CreditsConfig
    topup: TopupConfig
    cost_model: CostModelConfig

    @model_validator(mode="after")
    def validate_cross_refs(self):
        if self.trial.fallback_plan not in self.plans:
            raise ValueError("trial fallback_plan must reference an existing plan")
        return self
```

- [ ] **步骤 4：用当前冻结真值构造默认 payload**

`build_default_pricing_payload()` 的默认值来源：
- `gateway/plan_catalog.py`
- `gateway/credits_service.py`
- `gateway/admin_settings.py` 中的 `voice_clone_cost_credits`
- V3 文档中的 `topup` 和 `cost_model` 初始建议值

- [ ] **步骤 5：再次运行测试，确认转绿**

运行：
`C:\Users\Administrator\.local\bin\python.cmd -m pytest tests/test_pricing_schema.py -q`

预期：
PASS

- [ ] **步骤 6：提交**

```bash
git add gateway/pricing_schema.py tests/test_pricing_schema.py
git commit -m "feat: add pricing payload schema and defaults"
```

---

### 任务 2：新增 runtime snapshot 层

**涉及文件：**
- 新增：`gateway/pricing_runtime.py`
- 测试：`tests/test_pricing_runtime.py`

- [ ] **步骤 1：先写失败测试**

```python
def test_runtime_uses_snapshot_file_when_present(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "PRICING_RUNTIME_FILE", tmp_path / "pricing_runtime.json")
    runtime.write_runtime_snapshot(build_default_pricing_payload())
    payload = runtime.get_runtime_pricing(force_reload=True)
    assert payload.credits.voice_clone_cost_credits == 500


def test_runtime_falls_back_to_defaults_when_snapshot_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "PRICING_RUNTIME_FILE", tmp_path / "missing.json")
    payload = runtime.get_runtime_pricing(force_reload=True)
    assert payload.plans["plus"].monthly_grant_credits == 3500
```

- [ ] **步骤 2：运行测试，确认它先失败**

运行：
`C:\Users\Administrator\.local\bin\python.cmd -m pytest tests/test_pricing_runtime.py -q`

预期：
缺 runtime 模块或缺函数，测试失败

- [ ] **步骤 3：实现 runtime 缓存 helper**

实现：
- `get_runtime_pricing(force_reload: bool = False) -> PricingPayload`
- `write_runtime_snapshot(payload: PricingPayload) -> None`
- `invalidate_runtime_pricing_cache() -> None`

要求：
- 使用进程内缓存
- 只能显式失效
- V1 不做后台自动刷新

- [ ] **步骤 4：保持 runtime 读取同步**

同步读取来源只允许是：
- `PRICING_RUNTIME_FILE = /opt/aivideotrans/config/pricing_runtime.json`
- fallback 到 `build_default_pricing_payload()`

不要让同步运行时代码直接查数据库。

- [ ] **步骤 5：再次运行测试，确认转绿**

运行：
`C:\Users\Administrator\.local\bin\python.cmd -m pytest tests/test_pricing_runtime.py -q`

预期：
PASS

- [ ] **步骤 6：提交**

```bash
git add gateway/pricing_runtime.py tests/test_pricing_runtime.py
git commit -m "feat: add pricing runtime snapshot layer"
```

---

### 任务 3：新增 pricing version 表和 seed 发布路径

**涉及文件：**
- 修改：`gateway/models.py`
- 修改：`gateway/main.py`
- 新增：`gateway/alembic/versions/011_add_pricing_config_versions.py`
- 测试：`tests/test_pricing_admin.py`

- [ ] **步骤 1：先写失败测试**

```python
def test_seed_creates_active_pricing_version_when_table_empty(...):
    result = ensure_pricing_seeded(...)
    assert result.version == 1
    assert result.status == "active"


def test_publish_archives_previous_active_version(...):
    publish_pricing_version(...)
    active_rows = ...
    assert len(active_rows) == 1
    assert archived_rows[0].status == "archived"
```

- [ ] **步骤 2：运行测试，确认它先失败**

运行：
`C:\Users\Administrator\.local\bin\python.cmd -m pytest tests/test_pricing_admin.py -q -k "seed or publish"`

预期：
缺表、缺模型或缺 helper，测试失败

- [ ] **步骤 3：新增 SQLAlchemy 模型**

```python
class PricingConfigVersion(Base):
    __tablename__ = "pricing_config_versions"
    id = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    version = mapped_column(Integer, nullable=False)
    status = mapped_column(String(16), nullable=False)
    payload_json = mapped_column(JSONB, nullable=False)
    change_note = mapped_column(Text, nullable=True)
    updated_by_user_id = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    created_at = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    activated_at = mapped_column(DateTime(timezone=True), nullable=True)
```

- [ ] **步骤 4：新增 Alembic migration**

创建 `011_add_pricing_config_versions.py`，包含：
- 新表
- `status`、`version`、`created_at` 索引
- 不做破坏性数据迁移

- [ ] **步骤 5：启动时自动 seed active pricing**

在 `gateway/main.py` 的 lifespan 中：
- 如果没有 active 版本，则插入 version `1`
- 内容来自 `build_default_pricing_payload()`
- 同时写 runtime snapshot 文件

要求：
- 本地 clean 环境如果 DB 不可用，不要直接让应用起不来
- 应记录警告日志，并 fallback 到 runtime 默认值

- [ ] **步骤 6：再次运行测试，确认转绿**

运行：
`C:\Users\Administrator\.local\bin\python.cmd -m pytest tests/test_pricing_admin.py -q -k "seed or publish"`

预期：
PASS

- [ ] **步骤 7：提交**

```bash
git add gateway/models.py gateway/main.py gateway/alembic/versions/011_add_pricing_config_versions.py tests/test_pricing_admin.py
git commit -m "feat: add pricing version table and seed path"
```

---

### 任务 4：新增 admin pricing API

**涉及文件：**
- 新增：`gateway/pricing_admin.py`
- 修改：`gateway/main.py`
- 测试：`tests/test_pricing_admin.py`

- [ ] **步骤 1：先写失败测试**

```python
def test_get_admin_pricing_returns_active_payload(client, admin_cookie):
    resp = client.get("/api/admin/pricing", cookies=admin_cookie)
    assert resp.status_code == 200
    assert resp.json()["active"]["payload"]["plans"]["plus"]["price_cny_fen"]["monthly"] == 9900


def test_save_draft_does_not_change_runtime_snapshot(client, admin_cookie):
    resp = client.post("/api/admin/pricing/draft", json={"payload": edited_payload}, cookies=admin_cookie)
    assert resp.status_code == 200
    assert get_runtime_pricing(force_reload=True).plans["plus"].price_cny_fen.monthly == 9900


def test_publish_updates_runtime_snapshot(client, admin_cookie):
    resp = client.post("/api/admin/pricing/publish", json={"payload": edited_payload, "change_note": "raise plus"}, cookies=admin_cookie)
    assert resp.status_code == 200
    assert get_runtime_pricing(force_reload=True).plans["plus"].price_cny_fen.monthly == 10900
```

- [ ] **步骤 2：运行测试，确认它先失败**

运行：
`C:\Users\Administrator\.local\bin\python.cmd -m pytest tests/test_pricing_admin.py -q`

预期：
缺 router 或缺 endpoint，测试失败

- [ ] **步骤 3：实现 admin-only 路由**

实现：
- `GET /api/admin/pricing`
- `GET /api/admin/pricing/history`
- `POST /api/admin/pricing/draft`
- `POST /api/admin/pricing/publish`

行为约束：
- 保存 draft：新增一条 `draft` 记录
- publish：归档旧 active，写入新 active，落 runtime snapshot，失效缓存
- history：按时间倒序返回摘要列表
- 如果 publish payload 改动了冻结字段，必须要求非空 `change_note`
- API 层要能识别“冻结字段是否被改动”

- [ ] **步骤 4：复用现有 admin 鉴权风格**

对齐 `admin_settings.py` 的模式：
- 使用 `get_current_user`
- 只允许 admin
- 返回中文优先的错误文案

- [ ] **步骤 5：再次运行测试，确认转绿**

运行：
`C:\Users\Administrator\.local\bin\python.cmd -m pytest tests/test_pricing_admin.py -q`

预期：
PASS

- [ ] **步骤 6：提交**

```bash
git add gateway/pricing_admin.py gateway/main.py tests/test_pricing_admin.py
git commit -m "feat: add admin pricing draft and publish api"
```

---

### 任务 5：把现有 pricing 消费方改成 runtime 真值派生

**涉及文件：**
- 修改：`gateway/plan_catalog.py`
- 修改：`gateway/credits_service.py`
- 修改：`gateway/billing.py`
- 修改：`gateway/job_intercept.py`
- 修改：`gateway/voice_selection_api.py`
- 修改：`gateway/admin_settings.py`
- 修改：`src/pipeline/process.py`
- 修改：`frontend-next/src/app/(app)/admin/settings/page.tsx`
- 测试：`tests/test_plan_catalog.py`
- 测试：`tests/test_billing.py`
- 测试：`tests/test_gateway_job_policy.py`
- 测试：`tests/test_gateway_create_job.py`
- 测试：`tests/test_voice_selection_pricing.py`
- 测试：`tests/test_voice_selection_payload.py`

本任务 blast radius 最大，必须拆成 4 个可独立提交的小任务，避免一次性改 5 条 import 链。

#### 任务 5a：先改 plan_catalog

- [ ] **步骤 1：先写失败测试**

```python
def test_plan_catalog_reads_runtime_payload(monkeypatch):
    monkeypatch.setattr(runtime, "get_runtime_pricing", lambda force_reload=False: payload_with_plus_109)
    assert get_price("plus", "monthly") == 10900


def test_estimate_credits_reads_runtime_debit_rate(monkeypatch):
    monkeypatch.setattr(runtime, "get_runtime_pricing", lambda force_reload=False: payload_with_express_12)
    assert estimate_credits(estimated_minutes=5, service_mode="express", quality_tier="standard") == 60
```

- [ ] **步骤 2：运行 plan_catalog 聚焦测试，确认它先失败**

运行：
`C:\Users\Administrator\.local\bin\python.cmd -m pytest tests/test_plan_catalog.py -q`

预期：
因为模块仍在用 import-time 冻结常量而失败

- [ ] **步骤 3：把 plan catalog 重构为 runtime accessor**

改造为通过 helper 获取当前值：
- `get_runtime_plan_definitions()`
- `get_runtime_trial_config()`
- `get_price(...)`
- `valid_target_plan_codes()`
- `get_legacy_plan_gate_dict()`
- `get_legacy_price_table()`

要求：
- 尽量保持现有 public 函数名不变
- 让旧调用方尽量无感迁移

- [ ] **步骤 4：再次运行 plan_catalog 测试，确认转绿**

运行：
`C:\Users\Administrator\.local\bin\python.cmd -m pytest tests/test_plan_catalog.py -q`

预期：
PASS

- [ ] **步骤 5：提交 5a**

```bash
git add gateway/plan_catalog.py tests/test_plan_catalog.py
git commit -m "refactor: derive plan catalog from runtime pricing"
```

#### 任务 5b：再改 credits_service

- [ ] **步骤 1：把 credits service 重构为 runtime accessor**

将 live 逻辑改为读取：
- `get_debit_rates()`
- `get_grant_amounts()`
- `get_bucket_priority()`

兼容要求：
- 如果测试仍然 import 旧常量，可以保留兼容 alias
- 但要明确它只是 fallback snapshot，不再是唯一真值

- [ ] **步骤 2：运行 credits / voice pricing 聚焦测试，确认行为正确**

运行：
`C:\Users\Administrator\.local\bin\python.cmd -m pytest tests/test_voice_selection_pricing.py -q`

预期：
PASS

- [ ] **步骤 3：提交 5b**

```bash
git add gateway/credits_service.py tests/test_voice_selection_pricing.py
git commit -m "refactor: derive credits service from runtime pricing"
```

#### 任务 5c：清理 billing 和 job_intercept 的 request-time 真值读取

- [ ] **步骤 1：清理 billing 和 job_intercept 里的 import-time 冻结依赖**

具体修正：
- `gateway/billing.py`
  - 不再用 module-level `VALID_TARGET_PLANS` 做 request-time 校验
  - 不再用 module-level `PLAN_PRICES_CNY` 作为 request-time 真值
- `gateway/job_intercept.py`
  - 不再依赖 module-level `PLAN_CATALOG` 做匿名 fallback
  - 请求时动态计算 free fallback

- [ ] **步骤 2：运行 billing / gateway policy / create-job 测试**

运行：
- `C:\Users\Administrator\.local\bin\python.cmd -m pytest tests/test_billing.py -q`
- `C:\Users\Administrator\.local\bin\python.cmd -m pytest tests/test_gateway_job_policy.py tests/test_gateway_create_job.py -q`

预期：
PASS

- [ ] **步骤 3：提交 5c**

```bash
git add gateway/billing.py gateway/job_intercept.py tests/test_billing.py tests/test_gateway_job_policy.py tests/test_gateway_create_job.py
git commit -m "refactor: resolve billing and job policy from runtime pricing"
```

#### 任务 5d：补齐 voice selection 和 process.py 的 clone cost 真值迁移

- [ ] **步骤 1：让 voice selection pricing 读取 runtime credits truth**

`gateway/voice_selection_api.py` 应读取：
- clone cost：来自 runtime credits config
- 每分钟点数：来自 runtime debit rate config

- [ ] **步骤 2：修正 `process.py` 中的 `clone_cost_credits` 硬编码**

当前 [process.py](/D:/Claude/AIVideoTrans_Codex_web_mvp/src/pipeline/process.py#L1938) 仍写死：

```python
"clone_cost_credits": 500
```

需要改为从 `pricing_runtime.get_runtime_pricing()` 读取。

- [ ] **步骤 3：迁移旧 admin_settings 字段**

在 `gateway/admin_settings.py` 中：
- 保留 `voice_clone_cost_credits` 字段以兼容老配置文件
- 标记为 deprecated
- 注明它不再作为 live pricing 真值来源

在 `frontend-next/src/app/(app)/admin/settings/page.tsx` 中：
- 不再提供该字段的编辑入口
- 或显示“该字段已迁移到定价管理”提示

- [ ] **步骤 4：补 `process.py` 的 payload 测试**

在 `tests/test_voice_selection_payload.py` 中增加断言：
- `clone_cost_credits` 从 runtime pricing 派生
- 不再使用硬编码 `500`

- [ ] **步骤 5：运行 voice selection / process payload 聚焦测试**

运行：
- `C:\Users\Administrator\.local\bin\python.cmd -m pytest tests/test_voice_selection_pricing.py tests/test_voice_selection_payload.py -q`

预期：
PASS

- [ ] **步骤 6：提交 5d**

```bash
git add gateway/voice_selection_api.py gateway/admin_settings.py src/pipeline/process.py frontend-next/src/app/(app)/admin/settings/page.tsx tests/test_voice_selection_pricing.py tests/test_voice_selection_payload.py
git commit -m "refactor: migrate voice selection pricing to runtime truth"
```

---

### 任务 6：构建后台定价管理页面

**涉及文件：**
- 新增：`frontend-next/src/app/(app)/admin/pricing/page.tsx`
- 新增：`frontend-next/src/lib/admin/pricing.ts`
- 修改：`frontend-next/src/components/app-shell.tsx`

- [ ] **步骤 1：先写失败测试或 smoke checklist**

如果当前前端测试体系没有覆盖 admin page，可以先加一个最小 fetch helper 单测；如果暂时不适合补测试，就在 PR 中保留手工 smoke checklist：
- admin 可正常打开页面
- 非 admin 会看到 forbidden
- draft 保存可用
- publish 可用
- history 可渲染

示例 helper test：

```ts
it("normalizes admin pricing payload", async () => {
  const data = await getAdminPricing(fetchMock)
  expect(data.active.payload.trial.days).toBe(7)
})
```

- [ ] **步骤 2：运行前端测试或记录当前 red 状态**

如果加了测试就跑测试；否则至少记录“当前页面不存在”这一 red 状态。

- [ ] **步骤 3：实现 typed admin pricing client**

在 `frontend-next/src/lib/admin/pricing.ts` 中实现：
- `getAdminPricing()`
- `savePricingDraft()`
- `publishPricing()`
- `listPricingHistory()`

- [ ] **步骤 4：实现页面 5 个分区**

页面分区：
- 套餐与 Trial
- 点数策略
- Top-up 点数包
- 成本校准
- 发布与版本历史

行为要求：
- 显示 `catalog_frozen` 状态 badge
- 冻结字段默认只读
- 冻结字段显示锁图标
- 修改冻结字段必须经过二次确认
- 如果 draft 或 publish 包含冻结字段变更，`change_note` 必填
- `topup` 和 `cost_model` 可以编辑，但不等于自动启用购买流程
- “成本校准”分区明确标注：
  - “以下参数用于成本测算和 pilot 观测，不直接影响用户扣点”

- [ ] **步骤 5：增加后台导航入口**

在 `frontend-next/src/components/app-shell.tsx` 的后台菜单里加入：

```tsx
{ label: "定价管理", href: "/admin/pricing", icon: Wallet }
```

- [ ] **步骤 6：手工验证页面**

验证流程：
1. admin 登录
2. 打开 `/admin/pricing`
3. 修改 `voice_clone_cost_credits` 并保存 draft
4. 确认 active payload 没变
5. publish draft
6. 刷新页面，确认 history 和 active payload 都更新

- [ ] **步骤 7：提交**

```bash
git add frontend-next/src/app/(app)/admin/pricing/page.tsx frontend-next/src/lib/admin/pricing.ts frontend-next/src/components/app-shell.tsx
git commit -m "feat: add admin pricing management page"
```

---

### 任务 7：最终验证与部署说明

**涉及文件：**
- 仅验证，不新增文件

- [ ] **步骤 1：运行后端相关测试集**

运行：

```bash
C:\Users\Administrator\.local\bin\python.cmd -m pytest tests/test_pricing_schema.py tests/test_pricing_runtime.py tests/test_pricing_admin.py tests/test_plan_catalog.py tests/test_billing.py tests/test_gateway_job_policy.py tests/test_gateway_create_job.py tests/test_voice_selection_pricing.py -q
```

预期：
全部通过

- [ ] **步骤 2：运行前端测试或手工 smoke checklist**

至少确认：
- admin pricing 页面可打开
- 现有 admin settings 页面未受影响
- checkout 仍然能解析当前价格

- [ ] **步骤 3：在干净环境验证 migration 和 seed**

运行：

```bash
cd gateway
alembic upgrade head
```

然后确认：
- DB 中存在一条 active pricing row
- `/opt/aivideotrans/config/pricing_runtime.json` 已写出

- [ ] **步骤 4：验证 live runtime 传播**

发布一个修改过的 payload，例如修改：
- `voice_clone_cost_credits`
- `plus.monthly`

然后验证：
- `/api/admin/pricing` 返回新的 active 版本
- `/api/plans` 反映新的 plus 价格
- `/api/voice-selection/pricing` 反映新的 clone cost

- [ ] **步骤 5：提交最终集成修正**

```bash
git add -A
git commit -m "test: verify pricing admin integration end to end"
```

---

## 推荐上线顺序

1. 先上 DB 表 + runtime 默认值
2. 再上 admin API
3. 再改 runtime consumer
4. 最后上 admin 页面
5. 生产环境先保持 `topup.enabled = false`，直到 checkout 流程真正落地

这样可以在不改变当前商业行为的前提下，把“可编辑、可发布、可回滚”的定价真值体系先搭起来。

## 重点风险

- `billing.py` 和 `job_intercept.py` 里现有 import-time 冻结视图
- clean local 环境下的启动 seed 行为
- 不小心让 `cost_model` 直接驱动 live 扣点
- draft 保存误改 active runtime 真值
- 冻结 catalog 值被无提示修改，缺少风险提醒或审计
- V3 pilot 期间错误 publish `credits.debit_rates`，导致 shadow 观测口径前后不一致

## 明确非目标

- 不做 live Top-up 购买闭环
- 不做面向用户的 campaign / promo pricing
- 不做 per-user pricing
- 不替换现有 admin settings 页面
