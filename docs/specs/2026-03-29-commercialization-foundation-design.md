# 商业化前置能力设计

> **Status:** active (current source of truth — QUICKSTART 列为"商业化唯一业务依据")  
> **Last updated:** 2026-03-29（原稿）/ 2026-04-17（metadata 规整）  
> **Scope:** AIVideoTrans Web MVP / Gateway / Job API / Process Pipeline

## 1. 背景

当前仓库已经具备以下与商业化直接相关的基础能力：

- Gateway 已有 `plan_code` / `role` / `service_mode` / job snapshot 的雏形
- Job API 与 Job Store 已能保存 `service_mode`、`tts_provider`、`tts_model`、`plan_code_snapshot` 等字段
- `process.py` 已开始按任务快照消费执行策略，而不是完全依赖全局免费/付费开关
- 前端已有“快捷版 / 专业版”任务方案 UI 雏形

但当前仍存在几个阻碍上线的问题：

- 前端创建任务时仍硬编码 `service_mode='express'`，工作台版没有真正打通
- 免费额度字段已进入模型，但尚未形成完整的预扣、回滚、审计闭环
- 会员规则、任务方案、支付接入仍分散在多个文档和局部实现中
- 测试基线存在非隔离脚本，默认 `pytest -q` 不能稳定作为产品化验收入口

本设计的目标是补齐“会员体系、多用户约束、支付接入前置层”，让任务创建、执行、升级降级、额度扣减具备一致、可审计、可扩展的行为。

## 2. 目标与非目标

### 2.1 目标

1. 建立统一的会员模型：`plan_code + role + service_mode`
2. 建立 Gateway 单点决策模型，任务创建时一次性计算执行快照
3. 支持多用户并发场景下的时长限制、并发限制、免费额度限制
4. 为支付接入预留稳定数据模型和 API 边界
5. 保证已创建任务不受后续升级、降级、退款的动态影响

### 2.2 非目标

- 本阶段不做复杂按分钟精细计费
- 本阶段不重写 Python pipeline 主链路
- 本阶段不把支付逻辑下沉到 `process.py`
- 本阶段不处理剪映工程兼容性问题
- 本阶段不实现团队组织、邀请协作、企业发票等扩展能力

### 2.3 概念词汇表

为避免“套餐 / 角色 / 方案”继续混用，本设计统一以下术语：

| 英文 | 中文 | 值域 | 说明 |
| --- | --- | --- | --- |
| `plan_code` | 套餐 | `free` / `plus` / `pro` | 用户当前购买或分配到的权益等级 |
| `role` | 角色 | `user` / `admin` | 用户系统权限，决定是否拥有后台与豁免能力 |
| `service_mode` | 任务方案 | `express` / `studio` | 单个任务的运行模式，由用户在创建任务时选择 |

其中：

- `plan_code` 决定“能不能创建某种任务”
- `service_mode` 决定“该任务按什么策略运行”
- `role` 决定“是否拥有管理员豁免和后台权限”

## 3. 设计原则

### 3.1 会员权益与执行策略解耦

- 会员层决定“用户有权创建什么任务”
- 任务方案层决定“任务如何运行”
- 执行策略层决定“具体跑哪个 provider / model / review flow”

前端只选任务方案，不直接决定具体 TTS provider。

### 3.2 Gateway 是唯一商业规则入口

所有套餐校验、并发校验、额度校验、任务策略计算，都只在 Gateway 做。

Job API、Job Store、Pipeline 只消费 Gateway 产出的结果，不重复推断业务规则。

### 3.3 任务快照不可变

任务一旦创建，以下字段就冻结：

- `service_mode`
- `tts_provider`
- `tts_model`
- `requires_review`
- `voice_clone_enabled`
- `voice_strategy`
- `plan_code_snapshot`
- `role_snapshot`
- `quota_cost`

后续用户升级、降级、退款只影响新任务，不影响旧任务。

### 3.4 配额采用“预扣 + 结算”

如果只在任务完成时扣减额度，多任务并发会穿透限制。

因此必须采用：

- 创建任务时预扣
- 任务完成时确认结算
- 任务失败、取消或校验失败时回滚

第一阶段实现不引入完整账本，而是使用：

- `users.free_jobs_quota_used`
- `jobs.quota_state`

先完成最小可用闭环，再在支付阶段升级为完整账本模型。

### 3.5 支付只改权益，不碰运行中任务

支付系统只负责：

- 创建订单
- 接收回调
- 修改用户当前权益
- 记录审计与账务事件

支付系统不直接修改已有任务快照。

## 4. 产品模型

### 4.1 账号层

#### 角色

- `user`
- `admin`

#### 套餐

- `free`
- `plus`
- `pro`

`admin` 是角色，不是套餐。管理员可以同时保留展示用 `plan_code`，但业务判断优先看 `role`。

### 4.2 任务方案层

#### `express`

- 面向低成本、全自动、快速完成
- 不进入人工审核链路
- 不启用音色克隆

#### `studio`

- 面向更高质量、可审校、可克隆音色
- 进入审核工作台
- 支持用户选择或克隆音色

### 4.3 套餐权益矩阵

| plan_code | 最大单任务时长 | 最大活跃任务数 | 可用方案 | 免费额度 |
| --- | --- | --- | --- | --- |
| `free` | 10 分钟 | 1 | `express` | 5 条 |
| `plus` | 60 分钟 | 3 | `express`, `studio` | 无免费条数限制 |
| `pro` | 180 分钟 | 10 | `express`, `studio` | 无免费条数限制 |

管理员豁免：

- 无时长上限
- 无并发上限
- 无免费额度限制
- 可访问后台管理接口

### 4.4 方案到执行策略的映射

#### `express`

- `tts_provider = cosyvoice`
- `tts_model = cosyvoice-v3-flash`
- `requires_review = false`
- `voice_clone_enabled = false`
- `voice_strategy = preset_mapping`

#### `studio`

- `plus` -> `tts_provider = minimax`, `tts_model = speech-2.8-turbo`
- `pro/admin` -> `tts_provider = minimax`, `tts_model = speech-2.8-hd`
- `requires_review = true`
- `voice_clone_enabled = true`
- `voice_strategy = user_selected`

### 4.5 前端命名规范

前端命名建议固定为：

- 方案名：`快捷版` / `工作台版`
- 套餐名：`Free` / `Plus` / `Pro`

不要继续把“专业版”同时用于套餐和任务方案，以避免语义冲突。

## 5. 系统边界

统一数据流：

`Frontend -> Gateway -> PostgreSQL + Job API -> Job Store -> Process Pipeline`

### 5.1 Frontend

职责：

- 展示当前用户权益
- 收集任务输入
- 让用户选择 `service_mode`
- 调用 Gateway 暴露的业务接口

不负责：

- 推断最终 TTS provider
- 推断是否允许创建任务
- 计算配额扣减规则

### 5.2 Gateway

职责：

- 鉴权
- 读取用户 `role` 和 `plan_code`
- 校验任务创建权限
- 基于估算值或轻量 probe 做初步时长判断
- 计算任务执行快照
- 预扣额度
- 持久化 Postgres 元数据
- 透传快照到 Job API

这是唯一商业规则入口。

### 5.3 Job API / Job Store

职责：

- 保存任务记录
- 保存任务事件
- 返回任务状态、日志、产物

不负责：

- 套餐规则判断
- 支付规则判断
- 动态更改任务权益

### 5.4 Process Pipeline

职责：

- 按任务快照执行任务
- 产出日志、状态、审校、草稿与结果文件

不负责：

- 再次判断“当前是不是免费用户”
- 再次读取支付或订阅状态

## 6. 数据模型

本节给出推荐落地的数据表结构。分为：

- Phase 0+4 必须落地的核心表
- Phase 5+ 再引入的账本与支付表

### 6.0 Alembic 迁移策略

第一阶段必须补一版明确的 Alembic 迁移，建议命名为：

- `002_commercialization_foundation`

迁移原则：

- 对已有 `users` 表新增字段时统一提供 `server_default`
- 对已有 `jobs` 表新增字段时优先使用 `nullable=True` 或安全默认值
- 不对历史任务反推不存在的套餐和 provider 快照
- 不通过 ORM 自动建表替代 migration

建议迁移内容：

| 表 | 字段 | 建议默认值 |
| --- | --- | --- |
| `users` | `role` | `'user'` |
| `users` | `plan_code` | `'free'` |
| `users` | `free_jobs_quota_total` | `5` |
| `users` | `free_jobs_quota_used` | `0` |
| `jobs` | `quota_state` | `'none'` |
| `jobs` | `quota_cost` | `1` |
| `jobs` | `create_idempotency_key` | `NULL` |

已有数据迁移策略：

- 已有用户自动视为 `role='user'`、`plan_code='free'`
- 管理员账户在迁移后手动设置 `role='admin'`
- 已有历史任务只补结构，不回填猜测型策略字段

这能避免把“历史运行事实”和“新商业规则”强行混在一起。

### 6.1 `users`

当前表已具备部分字段，建议保留并补齐审计字段。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | UUID PK | 用户 ID |
| `email` | VARCHAR(255) UNIQUE | 登录邮箱 |
| `display_name` | VARCHAR(128) | 显示名 |
| `password_hash` | VARCHAR(255) | 密码哈希 |
| `is_active` | BOOLEAN | 是否启用 |
| `role` | VARCHAR(16) | `user` / `admin` |
| `plan_code` | VARCHAR(16) | `free` / `plus` / `pro` |
| `free_jobs_quota_total` | INTEGER | 免费总额度，默认 5 |
| `free_jobs_quota_used` | INTEGER | 已使用额度 |
| `plan_updated_at` | TIMESTAMPTZ NULL | 最近一次套餐变更时间 |
| `created_at` | TIMESTAMPTZ | 创建时间 |
| `updated_at` | TIMESTAMPTZ | 更新时间 |

说明：

- 第一阶段仍允许直接在 `users` 表上表达当前套餐
- 支付接入后，也保留该字段作为“当前有效权益快照”

### 6.2 `jobs`

当前表已有大部分字段，建议补齐任务快照、幂等和轻量配额状态。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | UUID PK | DB 主键 |
| `job_id` | VARCHAR(64) UNIQUE | Job API 任务 ID |
| `user_id` | UUID FK | 所属用户 |
| `source_type` | VARCHAR(32) | `youtube_url` / `local_video` / `local_audio` |
| `source_ref` | TEXT | 视频来源 |
| `title` | VARCHAR(512) | 标题 |
| `status` | VARCHAR(32) | 任务状态 |
| `current_stage` | VARCHAR(64) NULL | 当前阶段 |
| `project_dir` | TEXT NULL | 项目目录 |
| `service_mode` | VARCHAR(16) NULL | `express` / `studio` |
| `tts_provider` | VARCHAR(32) NULL | 供应商快照 |
| `tts_model` | VARCHAR(64) NULL | 模型快照 |
| `requires_review` | BOOLEAN NULL | 是否进入审核 |
| `voice_clone_enabled` | BOOLEAN NULL | 是否允许克隆 |
| `voice_strategy` | VARCHAR(32) NULL | `preset_mapping` / `user_selected` |
| `plan_code_snapshot` | VARCHAR(16) NULL | 创建时套餐 |
| `role_snapshot` | VARCHAR(16) NULL | 创建时角色 |
| `source_duration_seconds` | FLOAT NULL | 源视频时长 |
| `estimated_duration_seconds` | FLOAT NULL | 创建时估算时长 |
| `quota_cost` | INTEGER NULL | 本次预扣额度 |
| `quota_state` | VARCHAR(16) NULL | `none` / `reserved` / `committed` / `released` |
| `create_idempotency_key` | VARCHAR(128) NULL UNIQUE | 创建任务幂等键 |
| `created_at` | TIMESTAMPTZ | 创建时间 |
| `updated_at` | TIMESTAMPTZ | 更新时间 |
| `started_at` | TIMESTAMPTZ NULL | 开始时间 |
| `completed_at` | TIMESTAMPTZ NULL | 完成时间 |

说明：

- `jobs` 保存的是“面向运行时的业务快照”
- 第一阶段不引入完整账本，而使用 `users.free_jobs_quota_used + jobs.quota_state` 实现轻量预扣与回滚
- `quota_state=reserved` 表示已预扣但尚未最终结算
- `quota_state=committed` 表示任务最终消耗该额度
- `quota_state=released` 表示已回滚，不能再次释放

### 6.3 `admin_audit_logs`

Phase 4 需要最小审计表，记录管理员对用户权益的人工修改。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | UUID PK | 审计记录 ID |
| `admin_user_id` | UUID FK | 执行操作的管理员 |
| `target_user_id` | UUID FK | 被修改的用户 |
| `action` | VARCHAR(32) | `change_plan` / `change_role` / `adjust_quota` |
| `before_state` | JSONB | 修改前快照 |
| `after_state` | JSONB | 修改后快照 |
| `reason` | TEXT NULL | 备注 |
| `created_at` | TIMESTAMPTZ | 创建时间 |

### 6.4 Phase 5+ 扩展：`usage_ledger`

完整账本表不是第一阶段必须项，建议在以下条件出现后再引入：

- Plus / Pro 需要按分钟计费
- 需要赠送分钟包或退款
- 需要财务对账而不只是 Free 条数校验

届时再新增 `usage_ledger`，用于：

- 创建任务时写一条 `reserve`
- 任务成功后写一条 `commit`
- 任务失败/取消后写一条 `release`
- 管理员补偿额度写 `adjust`
- 套餐赠送额度写 `grant`

第一阶段不要为了 5 条 Free 配额过早引入完整账本。

### 6.5 Phase 5+ 扩展：`payment_orders`

支付表保留在总设计中，但不属于 Phase 0+4 的实现范围。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | UUID PK | 本地订单 ID |
| `user_id` | UUID FK | 所属用户 |
| `provider` | VARCHAR(32) | `alipay` / `wechatpay` / `stripe` / `lemonsqueezy` |
| `provider_order_id` | VARCHAR(128) NULL UNIQUE | 第三方订单号 |
| `target_plan_code` | VARCHAR(16) | 购买目标套餐 |
| `billing_period` | VARCHAR(16) | `monthly` / `quarterly` / `annual` |
| `amount_cny` | INTEGER | 金额，单位分 |
| `currency` | VARCHAR(8) | 默认 `CNY` |
| `status` | VARCHAR(16) | `created` / `pending` / `paid` / `failed` / `cancelled` / `expired` / `refunded` |
| `checkout_url` | TEXT NULL | 支付链接或收银台链接 |
| `expires_at` | TIMESTAMPTZ NULL | 过期时间 |
| `paid_at` | TIMESTAMPTZ NULL | 支付成功时间 |
| `metadata` | JSONB | 扩展信息 |
| `created_at` | TIMESTAMPTZ | 创建时间 |
| `updated_at` | TIMESTAMPTZ | 更新时间 |

### 6.6 Phase 5+ 扩展：`payment_webhook_events`

该表也延后到支付阶段再实现。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | UUID PK | 本地事件 ID |
| `provider` | VARCHAR(32) | 支付渠道 |
| `provider_event_id` | VARCHAR(128) UNIQUE | 第三方事件 ID |
| `event_type` | VARCHAR(64) | 回调类型 |
| `signature_valid` | BOOLEAN | 签名是否通过 |
| `processed` | BOOLEAN | 是否已处理 |
| `payload` | JSONB | 原始回调 |
| `error_message` | TEXT NULL | 处理失败原因 |
| `received_at` | TIMESTAMPTZ | 接收时间 |
| `processed_at` | TIMESTAMPTZ NULL | 完成处理时间 |

## 7. API 边界

### 7.1 前端读取身份与权益

#### `GET /auth/me`

该接口只返回当前登录态对应的身份信息，用于 App Shell、头像区、管理员角标等轻量展示。

建议返回：

```json
{
  "user": {
    "id": "uuid",
    "email": "user@example.com",
    "display_name": "Demo User",
    "role": "user",
    "created_at": "2026-03-29T08:00:00+08:00"
  }
}
```

职责边界：

- 返回身份信息
- 可包含 `role`
- 不承担套餐额度、可用方案、剩余额度等权益计算职责

#### `GET /api/me/entitlements`

返回当前用户的前端可见权益信息，用于新建任务页、用量页与支付升级引导。

返回字段建议：

```json
{
  "role": "user",
  "plan_code": "free",
  "limits": {
    "max_duration_minutes": 10,
    "max_concurrent_jobs": 1,
    "allowed_service_modes": ["express"],
    "free_jobs_quota_total": 5,
    "free_jobs_quota_used": 2,
    "free_jobs_quota_remaining": 3
  },
  "ui": {
    "show_admin_badge": false,
    "allow_upgrade": true
  }
}
```

用途：

- 新建任务页显示当前权益
- 锁定不可用方案
- 用量页展示额度

### 7.2 任务创建

#### `POST /api/jobs`

前端请求体建议：

```json
{
  "job_type": "localize_video",
  "source": {
    "type": "youtube_url",
    "value": "https://www.youtube.com/watch?v=..."
  },
  "output_target": "editor",
  "speakers": "auto",
  "transcription_method": "assemblyai",
  "service_mode": "express",
  "estimated_duration_seconds": 532
}
```

Gateway 内部处理顺序：

1. 鉴权
2. 读取用户权益
3. 校验 `service_mode`
4. 使用 `estimated_duration_seconds` 或轻量 metadata probe 做初步时长判断
5. 校验时长上限
6. 校验并发上限
7. 基于事务做轻量配额预扣
8. 计算执行快照
9. 写 `jobs`
10. 转发到 Job API

Gateway 转发给 Job API 的内部请求体：

```json
{
  "job_type": "localize_video",
  "source": {
    "type": "youtube_url",
    "value": "https://www.youtube.com/watch?v=..."
  },
  "output_target": "editor",
  "speakers": "auto",
  "transcription_method": "assemblyai",
  "service_mode": "express",
  "tts_provider": "cosyvoice",
  "tts_model": "cosyvoice-v3-flash",
  "requires_review": false,
  "voice_clone_enabled": false,
  "voice_strategy": "preset_mapping",
  "plan_code_snapshot": "free",
  "role_snapshot": "user",
  "estimated_duration_seconds": 532,
  "quota_cost": 1
}
```

### 7.3 错误响应格式

Gateway 不应只返回自然语言字符串，必须返回结构化错误码，方便前端做明确提示。

建议格式：

```json
{
  "error": "quota_exhausted",
  "message": "免费额度已用完",
  "detail": {
    "quota_total": 5,
    "quota_used": 5
  }
}
```

第一阶段至少覆盖以下错误码：

- `quota_exhausted`
- `concurrent_limit`
- `duration_limit`
- `service_mode_not_allowed`
- `plan_required`
- `invalid_source`
- `job_create_conflict`

### 7.4 内部源信息回调

#### `POST /api/internal/jobs/{job_id}/source-metadata`

该接口仅供内部流程使用，不对浏览器开放。

典型用途：

- 本地上传完成后补齐真实时长
- Pipeline S0 获取到真实视频 metadata 后回写

请求体建议：

```json
{
  "source_duration_seconds": 648.2,
  "title": "Example Video",
  "probe_status": "confirmed"
}
```

行为要求：

1. 更新 `jobs.source_duration_seconds`
2. 如果实际时长超出当前任务创建时允许的上限，并且角色不是 `admin`
3. 将任务标记为失败
4. 若 `jobs.quota_state='reserved'`，则只释放一次配额

时长探测实现建议：

- 本地上传优先复用上传阶段保存的 metadata
- YouTube URL 优先使用 `yt-dlp --dump-json --no-download`
- Gateway 对轻量 probe 设置 5 秒超时
- 超时或 probe 失败时，回退到 `estimated_duration_seconds` 做初步放行
- 最终以 Pipeline S0 回写的真实 metadata 作为硬校验依据

### 7.5 管理后台

#### `GET /api/admin/users`

分页查看用户、当前套餐、额度和任务数。

#### `PATCH /api/admin/users/{user_id}/entitlements`

允许管理员手动修改：

- `plan_code`
- `role`
- `free_jobs_quota_total`
- `free_jobs_quota_used`

要求：

- 每次修改写 `admin_audit_logs`
- 不直接修改运行中的任务快照

### 7.6 支付（Phase 5+）

#### `POST /api/billing/orders`

前端创建订单时只提交：

```json
{
  "target_plan_code": "plus",
  "billing_period": "monthly",
  "provider": "alipay"
}
```

#### `GET /api/billing/orders/{order_id}`

查询订单状态。

#### `POST /api/billing/webhooks/{provider}`

只供支付平台回调。

处理要求：

- 校验签名
- 幂等处理
- 写 `payment_webhook_events`
- 成功后更新 `payment_orders.status`
- 再更新用户权益

### 7.7 禁止暴露的能力

以下能力不应直接开放给前端：

- 直接修改任务快照
- 直接指定任意 `tts_provider`
- 直接增加用户额度
- 绕过 Gateway 直接写 Job API

## 8. 关键流程

### 8.1 任务创建流程

1. 前端读取 `GET /api/me/entitlements`
2. 用户选择 `service_mode`
3. 前端生成 `create_idempotency_key` 并调用 `POST /api/jobs`
4. Gateway 校验套餐、并发、初步时长和额度
5. Gateway 在一个事务中执行 Free 配额预扣
6. Gateway 写 `jobs.quota_state='reserved'`
7. Gateway 调 Job API 创建任务
8. 若 Job API 创建成功，任务进入 `queued`
9. 若 Job API 创建失败，则回滚配额并把 `quota_state` 置为 `released`

### 8.2 真实时长确认流程

1. 上传流程或 Pipeline S0 拿到真实 metadata
2. 内部调用 `POST /api/internal/jobs/{job_id}/source-metadata`
3. 更新 `jobs.source_duration_seconds`
4. 若真实时长超限，则任务失败并释放预扣配额

### 8.3 任务结束结算流程

#### 成功

- `jobs.quota_state: reserved -> committed`
- `users.free_jobs_quota_used` 不回退

#### 失败或取消

- 若 `jobs.quota_state='reserved'`，则执行一次回滚
- `jobs.quota_state: reserved -> released`
- `users.free_jobs_quota_used -= quota_cost`

### 8.4 套餐升级流程

#### 管理员手动升级

1. 管理员调用 `PATCH /api/admin/users/{id}/entitlements`
2. 更新 `users.plan_code`
3. 写 `admin_audit_logs`
4. 新任务按新套餐执行
5. 旧任务不受影响

#### 支付升级（Phase 5+）

1. 创建 `payment_orders`
2. 用户完成支付
3. 回调到 `payment_webhook_events`
4. 幂等确认订单
5. 更新 `users.plan_code`
6. 写套餐变更审计

## 9. 幂等与一致性规则

### 9.1 任务创建幂等

任务创建至少需要一个 `idempotency_key`，防止前端重复提交造成双重扣减。

建议来源：

- 前端生成一次性 UUID
- Gateway 将其写入 `jobs.create_idempotency_key`
- 对同一用户 + 同一 `create_idempotency_key` 的重复请求直接返回已有结果

### 9.2 回调幂等（Phase 5+）

支付回调必须以 `provider_event_id` 去重。

同一个回调重复到达时：

- 不重复升级套餐
- 不重复改订单状态
- 不重复写账务记录

### 9.3 任务与额度的一致性

第一阶段，任何时刻都必须满足：

- `users.free_jobs_quota_used` 与用户名下 `quota_state in ('reserved', 'committed')` 的任务数可对账
- `released` 状态的任务不能再次释放额度
- `committed` 状态的任务不能再回滚

建议增加定时对账任务：

- 找出 `reserved` 但任务已结束未结算的记录
- 找出 `released` 但仍被计入额度的记录
- 找出 `users.free_jobs_quota_used` 与任务聚合结果不一致的用户

## 10. 实施顺序

### Phase 0+1: 基线收口 + 权益只读与前端接线

目标：先让商业化开发建立在可信基线上，并让前端正确理解用户权益。

任务：

1. 清理或停止跟踪一次性 `tmp_*` 脚本和非隔离测试脚本
2. 明确默认测试入口为 `pytest tests -q`
3. 新增 Alembic `002_commercialization_foundation`
4. 将管理员判定从 email/display_name hack 改为真正的 `role='admin'`
5. 新增 `GET /api/me/entitlements`
6. 前端新建任务页接入权益展示
7. 前端真正提交 `service_mode`
8. 将前端“专业版”统一改名为“工作台版”
9. 补齐这一阶段的最小回归测试

### Phase 2: 任务创建闭环

目标：让多用户创建任务时的套餐规则真正生效。

任务：

1. Gateway 完成 `service_mode` 校验
2. Gateway 完成并发校验
3. Gateway 增加结构化错误码响应
4. Gateway 基于 `estimated_duration_seconds` 或轻量 probe 做初步时长校验
5. 对 YouTube metadata probe 使用 `yt-dlp --dump-json --no-download`，并设置 5 秒超时
6. Gateway 生成并写入完整任务快照
7. Job API / Job Store 补齐字段保存
8. 增加内部 source metadata 回调
9. Pipeline 清除 legacy `admin_settings` 的套餐判定逻辑

### Phase 3: 轻量配额闭环

目标：先用最小复杂度打通 Free 用户 5 条额度的预扣、回滚和对账。

任务：

1. 创建任务时以事务方式预扣 `users.free_jobs_quota_used`
2. 写入 `jobs.quota_state='reserved'`
3. 成功时转为 `committed`
4. 失败/取消时转为 `released`
5. 增加轻量对账脚本

### Phase 4: 管理后台

目标：支持运营与手工修复。

任务：

1. 用户列表 API 与页面展示套餐、角色、额度、活跃任务数
2. 管理员手动修改 `plan_code` / `role`
3. 管理员手动调整免费额度
4. 记录 `admin_audit_logs`

### Phase 5: 账本与支付前置能力

目标：在完成 beta 会员体系后，再引入账本与支付骨架。

任务：

1. 新增 `usage_ledger`
2. 新增 `payment_orders`
3. 新增 `payment_webhook_events`
4. 实现订单创建 API
5. 实现回调幂等框架
6. 支付成功后更新套餐

### Phase 6: 正式支付接入

建议顺序：

1. 先接一个最符合当前主体资质的渠道
2. 先支持套餐升级，不做复杂优惠券
3. 先支持月付
4. 成功后再补降级、退款、账单页

## 11. 测试策略

### 11.1 必测单元

- `compute_job_policy`
- `plan/service_mode` 权限判断
- 并发限制判断
- 额度预扣与回滚
- 结构化错误码映射
- `quota_state` 状态迁移

### 11.2 必测集成

- `free` 用户只能创建 `express`
- `plus/pro` 用户可以创建 `studio`
- 运行中升级/降级不影响历史任务
- 同一用户并发提交不能突破上限
- 双击创建任务不会双扣额度
- 任务失败或取消后只回滚一次额度

### 11.3 产品验收

以下五条全部通过，才算 Phase 0+4 的 beta 阶段完成：

1. `Free/Plus/Pro/Admin` 创建任务行为一致且可解释
2. 前端对不可用方案有明确提示
3. 任务快照创建后不可变
4. `users.free_jobs_quota_used` 与 `jobs.quota_state` 可以对账
5. 管理员权限、套餐修改和额度调整具备角色校验与审计

支付相关验收放到 Phase 5+ 单独执行。

## 12. 风险与缓解

### 12.1 风险：前端、Gateway、Job API 三层字段漂移

缓解：

- 所有快照字段收敛到单一 schema
- 为 `service_mode` 和 snapshot 字段补充回归测试

### 12.2 风险：创建任务时无法稳定获取视频时长

缓解：

- 创建时只做轻量预估或 metadata probe
- YouTube metadata probe 使用 `yt-dlp --dump-json --no-download` 并设置 5 秒超时
- 本地上传优先复用上传阶段保存的 metadata
- 超时或 probe 失败时回退到 `estimated_duration_seconds`
- Pipeline S0 再回写真实时长，作为最终校验依据
- 真实时长超限时失败并释放预扣额度

### 12.3 风险：免费额度出现双扣或漏扣

缓解：

- 所有 Free 配额操作放在同一事务中
- 使用 `create_idempotency_key`
- 以 `jobs.quota_state` 防止重复释放
- 增加轻量对账任务

### 12.4 风险：套餐升级后旧任务行为突变

缓解：

- 强制采用任务快照
- pipeline 只消费快照，不读取动态套餐

## 13. 与现有仓库的直接落点

### 13.1 重点修改文件

- `gateway/job_intercept.py`
- `gateway/models.py`
- `gateway/auth.py`
- `gateway/admin_settings.py`
- `gateway/main.py`
- `src/services/jobs/api.py`
- `src/services/jobs/models.py`
- `src/services/jobs/store.py`
- `src/pipeline/process.py`
- `frontend-next/src/app/translations/new/page.tsx`
- `frontend-next/src/lib/api/jobs.ts`
- `frontend-next/src/types/jobs.ts`

### 13.2 新增文件建议

- `gateway/alembic/versions/002_commercialization_foundation.py`
- `gateway/alembic/versions/003_admin_audit_logs.py`
- `gateway/admin_audit.py`
- `frontend-next/src/lib/api/entitlements.ts`
- `frontend-next/src/lib/api/billing.ts`（Phase 5+）

## 14. 结论

下一阶段不应该先冲支付按钮，而应该先把“权益判定 -> 任务快照 -> 轻量配额闭环”这条主链路打稳。

推荐执行顺序：

1. Phase 0+1：基线、迁移、权益只读、前端接线
2. Phase 2：任务创建闭环、错误码、任务快照、时长确认
3. Phase 3：轻量 Free 配额预扣/回滚与对账
4. Phase 4：管理员用户与权益管理
5. Phase 5：账本与支付骨架
6. Phase 6：正式支付接入

只有这样，支付上线后才不会把当前工程里的策略分叉和一致性问题放大。
