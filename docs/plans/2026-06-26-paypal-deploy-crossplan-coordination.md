# PayPal 收款上线 — 跨方案协调注记（2026-06-26）

> **目的**：给**任何工具/会话**（Claude Code / CodeX / 新 clone / 各 worktree）的 **git 可见**协调记录。
> Claude auto-memory（`.claude/projects/.../memory/`）只有 Claude Code 读得到；本文件是 git 跟踪的跨工具版本，内容与以下 auto-memory 对齐：`project_paypal_integration_status` / `project_ui_page_locale_plan` / `project_code_quality_plan_2026_06_24`。
> **最硬的协调通道始终是 git + docs/plans + 项目主**；auto-memory 是 Claude 侧的软层。

## TL;DR

PayPal（第四条收款轨，海外 USD 专轨）已**合并 main 并部署 prod，含 LIVE 真钱模式**。本文给两个**暂停中**的方案续接时的防冲突要点：
- **界面多语言 uiloc**（`docs/plans/2026-06-25-ui-page-locale-switch-plan.md`）
- **代码质量优化**（`docs/plans/2026-06-24-code-quality-efficiency-standards-optimization-plan.md` + MERGED 版）

## 1. main 上新增了什么（均未 push）

| commit | 内容 |
|---|---|
| `5be31e82` | PayPal 收款集成（provider 模块 + billing 接线 + geo 路由 + USD 标价 + 前端 + 测试） |
| `41626672` | 账单历史对 PayPal 发票显示**实扣 USD**（`charged_usd_cents`） |

- Plan：`docs/plans/2026-06-26-paypal-integration-plan.md`
- ⚠️ **本仓 `.git` 是 shallow + 多 worktree 共享**：push 绝不带 `--depth`（会发不完整 pack 被 reject，见 memory `feedback_shallow_repo_fetch_footgun`）。**项目主 ready 时 push**。

## 2. prod 部署态（美国主机，与"干净 main 部署"分叉）

- **gateway** = main HEAD（PayPal + USD 显示）+ `.env` `AVT_PAYPAL_ENV=live` + LIVE 凭证 → **真钱模式，api-m.paypal.com，LIVE OAuth 校验过**。
- **前端 next** = **pre-uiloc base + 2 个 surgical 部署的 USD 文件**（`order-history.tsx` / `get-order-history.ts`）。其余前端仍是 uiloc 之前的状态。
- **回滚物料**：镜像 tag `aivideotrans-gateway:pre-usd-bak` / `:pre-paypal-bak`、`aivideotrans-next:pre-usd-bak`；`/tmp/.env.bak-prelive`（旧 sandbox 配置）；`/tmp/fe-bak-usd/`。
- **一键下架 PayPal**：`.env` 设 `AVT_PAYPAL_ENABLED=false` + recreate gateway（微信/Paddle 不受影响）。
- **待项目主终验**：真金小额（买 + 退款）验 LIVE webhook + 退款链路。

## 3. PayPal 改动 / 新增的文件（冲突面）

**后端：**
- `gateway/billing.py`（增长）：create_order 落 B2 USD 快照、`get_checkout_config` geo 路由、`_serialize_invoice` 加 `charged_usd_cents`、`list_billing_history` 批量载 USD、`/api/billing/paypal/return` 端点、退款三函数、`_process_payment_event` capture-id 绑定。
- `gateway/payment_provider_paypal.py`（**新文件**）；`gateway/payment_providers.py`（`PayPalProvider` + 注册）；`gateway/plan_catalog.py` / `pricing_schema.py`（`price_usd_cents` 平行字段）；`gateway/startup_checks.py`（`validate_paypal_config`）；`gateway/main.py`（lifespan 接线）；`.env.example`（`AVT_PAYPAL_*`）。

**前端：**
- `checkout-card.tsx`（接 next-intl `billing` namespace + USD 提示）、`order-history.tsx`（PayPal 渠道标签 + `$X.XX` 显示，**仍内联 CJK、未 next-intl 化**）、`lib/billing/get-order-history.ts`（`charged_usd_cents` 类型）、`lib/billing/types.ts`、`lib/admin/pricing.ts` + `app/[locale]/(app)/admin/pricing/page.tsx`（USD 字段）。
- **新增 `billing` next-intl namespace**：`messages/{zh,en}/billing.json` + `i18n/request.ts`（import 列表）+ `global.d.ts`（Messages 类型）+ 动过 `scripts/cjk-baseline.json`。

**测试：** `tests/test_paypal_*.py`（新）、`tests/test_subscriptions.py`（USD 历史用例）、`tests/test_billing*.py`（更新）。

## 4. 给 **uiloc（界面多语言）** 方案续接的要点

- PayPal **已经做了一处 uiloc 式迁移**：新增 `billing` next-intl namespace（见 §3）。→ **续接单元务必从最新 main 切分支**：`i18n/request.ts` / `global.d.ts` / `scripts/cjk-baseline.json` **已含 `billing`**，迁移其它 namespace 时**别覆盖/丢掉 billing**。
- `order-history.tsx` 仍是**内联 CJK**（PayPal/USD 只加了 ASCII `"PayPal"` / `"$"`，未动其 CJK 基线）——uiloc 后续照常把它迁到 message keys。
- `admin/pricing/page.tsx` + `lib/admin/pricing.ts` 被加了 USD 字段（admin 本就 uiloc out-of-scope）。
- **部署顺序**：uiloc 将来部署整 main 前端时，会**干净覆盖** prod 的 pre-uiloc 前端并自动带上 PayPal 前端 + USD 显示。**从最新 main 部署即可，勿回退 PayPal**。

## 5. 给 **代码质量优化** 方案续接的要点

- 新文件 `gateway/payment_provider_paypal.py`（自包含 provider，<800 行门）+ `billing.py` 增长 → **file-size-guard 基线（TU-03）须从最新 main 重算**。
- `billing.py` 是金融模块；PayPal 已带 `tests/test_paypal_*`（366+ 测试绿）。
- 关联（**已完整修复并落本地 main，未 push**）：`gateway/credits_service.py` 的 `ensure_subscription_bucket_from_v2`（line 1655，`scalar_one_or_none()` 遇**重复 subscription bucket** 抛 `MultipleResultsFound`，**非致命** shadow 路径、不阻断支付/升级/积分）。三 commit 闭合：① 代码容忍多行 `6df6c68e` ② 去重诊断脚本（默认 dry-run）`973dc6e6` ③ **alembic 044** 三个 partial unique index 根治并发 dup（per-order / free·trial / no-order backfill）+ 模型 `__table_args__` 同步 + 契约测试 `ffa65a0d`（CodeX 复核补上第三个 backfill 并发 index）。生产 2026-06-27 全量巡检干净（0 幻影）→ 可直接 `alembic upgrade head`，**待合并 + 维护窗口**。与本方案 EH/健壮性主题同类，已审计其它 `scalar_one_or_none`（line 1643 由 partial unique index 兜底；line 499 CloneBillingEvent 是 smart-clone 另一类、未改）。

## 6. 一句话

PayPal 已 LIVE 上线、与两方案**无架构冲突**；唯一需注意的接口面是 **uiloc 的 i18n 共享文件已被 PayPal 动过（billing namespace）**——两方案续接都**从最新 main 切分支**即可避免冲突。
