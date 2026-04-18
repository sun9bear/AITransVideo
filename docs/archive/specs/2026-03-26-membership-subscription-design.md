# 会员订阅与任务方案设计

## 目标

为 AIVideoTrans 增加一套可产品化的会员订阅体系，并将“会员等级”和“任务方案”解耦：

- 会员等级决定用户可用权益
- 任务方案决定具体执行流程、TTS 模型与是否包含人工审核

本方案先支持“注册即免费会员 + 后台手动升级 Plus / Pro”，支付、价格与精细消耗规则后续接入。

## 当前业务前提

- 新注册用户默认为 `Free`
- `Free` 用户：
  - 单条视频时长上限 10 分钟
  - 总共 5 条免费任务额度
  - 同时进行任务上限 1 条
- `Plus` 用户：
  - 单条视频时长上限 60 分钟
  - 同时进行任务上限 3 条
  - 可使用快捷版和工作台版
- `Pro` 用户：
  - 单条视频时长上限 180 分钟
  - 同时进行任务上限 10 条
  - 可使用快捷版和工作台版

## 产品模型

### 一、账号等级与权限维度

建议将“会员套餐”和“管理员权限”拆成两个维度，而不是把管理员作为第四种订阅套餐。

原因：

- `Free / Plus / Pro` 是订阅权益
- `Admin` 是后台管理权限
- 管理员可能本身也对应某个会员套餐，但在业务上应拥有额外权限与限制豁免

建议定义：

#### 1. 会员套餐（plan_code）

- `free`
- `plus`
- `pro`

#### 2. 账号角色（role）

- `user`
- `admin`

说明：

- 普通用户默认：`role = user`
- 管理员账号：`role = admin`
- 管理员默认拥有平台最高权限与任务限制豁免
- 后续如果需要，也可以让管理员同时保留一个展示用 `plan_code`，但业务判断应优先看 `role`

会员套餐权益包括：

- 单条视频时长上限
- 同时进行任务数上限
- 免费额度或后续点数额度
- 可使用的任务方案范围

管理员额外权限包括：

- 可使用全部任务方案
- 无视频时长上限
- 无同时进行任务数上限
- 不受免费额度或点数限制
- 可访问后台设置
- 可管理所有用户与全局运行配置

### 二、任务方案

任务方案只负责“任务如何执行”，不直接等同于会员级别。

建议定义两个固定方案：

#### 1. 快捷版

前端文案：

`自动生成无需任何操作，快速便捷`

内部策略：

- `service_mode = express`
- `tts_provider = cosyvoice`
- `tts_model = cosyvoice-v3-flash`
- `voice_strategy = preset_mapping`
- `requires_review = false`
- `voice_clone_enabled = false`
- 跳过人工审核，走全自动流程

适用账号：

- `Free`
- `Plus`
- `Pro`
- `Admin`

#### 2. 工作台版

前端文案：

`翻译文稿可审核，可克隆音色，更高质量`

内部策略：

- `service_mode = studio`
- `tts_provider = minimax`
- `requires_review = true`
- `voice_clone_enabled = true`
- 走审核工作台流程

适用账号：

- `Plus`
- `Pro`
- `Admin`

说明：

前端建议将当前“专业版”改名为“工作台版”或“高质量版”，避免与 `Pro` 会员名称混淆。

## 推荐实现策略

推荐采用“两层模型”：

1. 会员层：控制时长、并发、额度、可用方案
2. 角色层：控制后台权限与是否豁免限制
3. 任务方案层：控制 TTS 模型、是否审核、是否支持克隆音色

推荐先做：

- 注册即 `Free`
- 后台手动升级用户到 `Plus / Pro`
- 单独维护 `Admin` 角色账号
- 先打通权益校验与任务执行策略
- 等价格、消耗规则确定后，再接支付与账单系统

不建议继续依赖全局 `admin_settings` 控制会员差异，因为平台上线后会同时存在不同等级的用户。

## 与当前代码的衔接

### 已有基础

- `gateway/job_intercept.py`
  - 已经在创建任务前做并发限制
  - 适合扩展成“按会员等级的任务创建校验”

- `src/pipeline/process.py`
  - 已经存在免费用户时长限制与全自动流程相关逻辑
  - 适合作为后续按任务策略执行的基础

- `gateway/admin_settings.py`
  - 已有 `tts_provider`、`skip_all_reviews_for_free_users`、`free_user_max_duration_minutes`
  - 但它们当前是全局配置，不适合多等级用户同时使用

- `frontend-next/src/app/translations/new/page.tsx`
  - 已有“快捷版 / 专业版”卡片样式雏形
  - 适合演进成会员感知的任务方案选择器

### 需要调整的方向

必须从“全局配置决定流程”升级为“任务创建时写入策略，任务运行时按策略执行”。

也就是：

1. 前端提交用户选择的任务方案
2. Gateway 根据当前用户会员等级做权限校验
3. Gateway 将最终执行策略固化到任务
4. Job API / pipeline 按任务自带策略执行，而不是按全局设置执行

### 数据流与职责分层

任务创建的数据流为：

```
frontend (service_mode) → Gateway compute_job_policy() → PostgreSQL + Job API → process.py
```

各层职责与对应文件：

1. **前端** (`frontend-next/src/lib/api/jobs.ts`)
   - `submitTranslationJob` 请求体增加 `service_mode` 字段
   - 不决定具体 provider

2. **Gateway** (`gateway/job_intercept.py`)
   - `compute_job_policy(user, service_mode)` 计算完整策略快照
   - 写入 PostgreSQL（Job 表）和上游 Job API 请求

3. **Job API** (`src/services/jobs/api.py`)
   - 接收 Gateway 传入的 snapshot 字段
   - 传递给 store 层持久化

4. **Job Store** (`src/services/jobs/models.py` + `src/services/jobs/store.py`)
   - `JobRecord` 扩展 snapshot 字段
   - store 持久化 snapshot 到 JSON 文件

5. **Pipeline** (`src/pipeline/process.py`)
   - 只消费快照，不推断用户等级或全局设置

Gateway 的 `compute_job_policy` 逻辑：
1. 查 user.role 和 user.plan_code
2. 根据 service_mode + plan_code + role 计算 tts_provider/tts_model/requires_review 等
3. 固化为 job snapshot 写入数据库

## 数据模型建议

### 方案 A：第一阶段最小实现

适合快速上线。

#### users 表新增字段

- `plan_code`
  - `free | plus | pro`
- `role`
  - `user | admin`
- `free_jobs_quota_total`
  - 默认 5
- `free_jobs_quota_used`
  - 默认 0

#### jobs 表新增字段

- `service_mode`
  - `express | studio`
- `plan_code_snapshot`
  - 创建任务时的会员等级快照
- `role_snapshot`
  - 创建任务时的角色快照
- `tts_provider`
  - `cosyvoice | minimax | mimo`
- `tts_model`
  - e.g. `cosyvoice-v3-flash`, `speech-2.8-turbo`, `speech-2.8-hd`
- `requires_review`
  - `true | false`
- `voice_clone_enabled`
  - `true | false`
- `voice_strategy`
  - `preset_mapping | user_selected`
- `quota_cost`
  - 先默认每条任务消耗 1
- `source_duration_seconds`
  - 用于审计与时长校验留痕

优点：

- 改动小
- 能快速支撑 Free / Plus / Pro
- 后续还能平滑升级到更完整模型

### 方案 B：长期标准实现

适合未来接支付与点数体系。

新增表建议：

- `subscriptions`
  - 存用户当前订阅状态与周期信息
- `usage_ledger`
  - 存额度消耗流水
- `plan_catalog` 或代码内固定 catalog
  - 存会员权益定义

当前阶段建议先做方案 A，后续再平滑升级。

## 核心后端能力

### 一、会员权益目录

在 Gateway 中维护一份明确的 plan catalog，例如：

- `free`
  - `max_duration_minutes = 10`
  - `max_concurrent_jobs = 1`
  - `allowed_service_modes = ["express"]`
  - `free_quota_total = 5`

- `plus`
  - `max_duration_minutes = 60`
  - `max_concurrent_jobs = 3`
  - `allowed_service_modes = ["express", "studio"]`

- `pro`
  - `max_duration_minutes = 180`
  - `max_concurrent_jobs = 10`
  - `allowed_service_modes = ["express", "studio"]`

另加一层角色规则：

- `admin`
  - `bypass_duration_limit = true`
  - `bypass_concurrency_limit = true`
  - `bypass_quota_limit = true`
  - `allowed_service_modes = ["express", "studio"]`
  - `can_manage_admin_settings = true`

### 二、创建任务前统一校验

在 Gateway 创建任务时统一做：

1. 当前用户属于哪个会员等级
2. 当前用户是否为管理员角色
3. 当前选择的任务方案是否允许
4. 视频时长是否超出等级上限
5. 当前进行中任务数是否超出上限
6. `Free` 用户剩余额度是否充足

如果不通过，直接返回明确错误信息给前端。

其中管理员逻辑应优先于套餐逻辑：

- 若 `role = admin`
  - 跳过时长限制
  - 跳过并发限制
  - 跳过额度限制
  - 允许全部任务方案

### 三、任务策略快照

任务创建成功时，要把最终策略写入任务快照：

- 会员等级
- 账号角色
- 任务方案
- TTS 提供商
- 是否需要审核
- 是否允许克隆音色
- 本次消耗额度

这样用户后续升级或降级时，已创建任务仍然按创建时策略执行。

### 四、额度消耗策略

当前阶段建议：

- `Free` 用户每创建 1 条任务，消耗 1 条免费额度
- `Plus / Pro` 先不做复杂扣减，只校验权限和并发

后续可升级为：

- 按任务方案消耗不同额度
- 按时长阶梯消耗
- 按分钟数消耗

因此从第一阶段开始，建议把“5 条免费任务”按“额度”来建模，而不是简单按 jobs 数量硬编码。

## 前端设计建议

### 新建翻译页

页面上增加三层信息：

#### 1. 当前会员信息

展示：

- 当前会员等级
- 当前账号角色（若为管理员）
- 当前时长上限
- 当前并发上限
- `Free` 用户剩余免费额度

例如：

- 免费会员：剩余 3 / 5 条，最长 10 分钟，同时进行 1 条
- 管理员：无限制，可使用全部方案，可访问后台设置

#### 2. 任务方案卡片

保留两个卡片：

- `快捷版`
- `工作台版`

表现规则：

- `Free` 用户：
  - 看得到两个卡片
  - 只能选 `快捷版`
  - `工作台版` 卡片禁用，但保留说明和升级提示

- `Plus / Pro` 用户：
  - 两个卡片都可选

- `Admin` 用户：
  - 两个卡片都可选
  - 页面可显示“管理员”标识
  - 不展示免费额度限制文案

#### 3. 限制与升级提示

当 `Free` 用户尝试点击 `工作台版` 时：

- 弹出升级提示
- 明确说明：
  - 工作台版支持翻译文稿审核
  - 支持克隆音色
  - 质量更高
  - Plus / Pro 可用

### 页面文案建议

- 快捷版：
  - `自动生成无需任何操作，快速便捷`

- 工作台版：
  - `翻译文稿可审核，可克隆音色，更高质量`

## 任务执行策略建议

### 快捷版

- TTS：`CosyVoice v3-flash`（预设音色映射，不复刻）
- 自动处理
- 跳过审核
- 用户不能编辑翻译文稿
- 用户不能克隆音色

### 工作台版

- TTS：`Minimax`
- 进入审核链路
- 用户可审核翻译文稿
- 用户可克隆音色

建议在前端传 `service_mode`，后端映射为具体执行配置，不让前端直接决定真实 TTS 供应商细节。

## 分阶段实施建议

### 第一阶段：不接支付，先上线权益系统

- 注册即 `Free`
- 后台可手动设置 `Plus / Pro`
- 后台可单独设置 `Admin` 角色
- 上线时长限制、并发限制、免费额度
- 前端显示会员权益与任务方案
- Free 可见但不可选工作台版

### 第二阶段：接订阅与升级入口

- 增加订阅页或账户页会员信息
- 增加升级入口
- 接支付回调
- 根据支付结果更新 `plan_code`

### 第三阶段：上线通用消耗系统

- 引入更细的 usage ledger
- 支持不同方案不同消耗
- 支持按时长计费或按额度扣减

## 风险与注意事项

### 一、不要继续依赖全局开关区分免费、付费与管理员

否则 Free、Pro、Admin 同时使用时，平台无法对不同用户执行不同流程。

### 二、额度必须留痕

如果只靠 jobs 数量判断“免费已用几条”，未来接支付和退款会很难扩展。

### 三、任务策略必须快照化

否则用户升级、降级、后台修改权益后，正在运行或历史任务行为会不一致。

### 五、快捷版不做音色复刻

这是架构约束，不是建议。快捷版的卖点是"快"，复刻会增加链路长度和失败面。
- 快捷版默认：只用系统预设音色
- 复刻：只留给工作台版
- MiMo 仅作为快捷版备用 provider，不承担默认生产链路

### 四、命名不要混淆

会员等级中的 `Pro`，不要与任务方案中的“专业版”共用同一名字。

建议：

- 会员：`Free / Plus / Pro`
- 方案：`快捷版 / 工作台版`

## 推荐实施顺序

1. 增加用户会员字段、角色字段与最小额度字段
2. 在 Gateway 增加 plan catalog、role rule 与创建前权益校验
3. 在 jobs 中增加任务策略快照字段
4. 前端新建翻译页增加会员信息、管理员标识与任务方案锁定态
5. pipeline 改为按任务策略执行，而不是按全局配置执行
6. 后台增加手动调整会员等级与管理员角色能力
7. 后续再接支付和精细消耗规则

### 改动目标文件清单

后端：
- `gateway/models.py` — User 加 role/plan_code/quota，Job 加策略快照字段
- `gateway/job_intercept.py` — compute_job_policy + 写入快照
- `gateway/admin_settings.py` — express/studio provider 配置
- `src/services/jobs/api.py` — Job API 接收 snapshot 字段
- `src/services/jobs/models.py` — JobRecord 同步扩展 snapshot 字段
- `src/services/jobs/store.py` — 持久化 snapshot 字段到 JSON
- `src/pipeline/process.py` — 消费快照，不再推断用户等级
- Alembic migration

前端：
- `frontend-next/src/lib/api/jobs.ts` — submitTranslationJob 增加 service_mode 字段
- `frontend-next/src/app/translations/new/page.tsx` — 任务方案选择 + 会员信息
- `frontend-next/src/app/admin/settings/page.tsx` — 后台 provider 配置

## 本阶段结论

本项目的会员订阅功能，建议先按“会员等级 + 任务方案”的双层模型实现。

当前阶段最合理的上线版本是：

- 注册即免费会员
- Free 用户可创建最长 10 分钟的视频任务
- Free 用户总共 5 条免费任务额度
- Free 用户最多同时进行 1 条任务
- Plus 用户最长 60 分钟，同时进行 3 条
- Pro 用户最长 180 分钟，同时进行 10 条
- Admin 用户可提交任意时长视频、无并发限制、无额度限制
- Free 用户仅可使用快捷版
- Plus / Pro 可使用快捷版和工作台版
- Admin 可使用快捷版和工作台版，并可访问后台设置
- 快捷版走 `CosyVoice v3-flash + 全自动流程 + 预设音色映射`（MiMo 仅作为备用）
- 工作台版走 `Minimax + 可审核流程 + 可克隆音色`

价格、支付、每条视频消耗规则后续单独接入，不阻塞第一阶段上线。
