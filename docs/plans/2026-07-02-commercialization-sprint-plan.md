# 2026-07-02 商业化冲刺方案（Commercialization Sprint，CM 系列）

> 决策记录：2026-07-02 项目主认可商业化评审方向（评审=8-agent 仓库核查 + 市场调研 + 主模型合成）。
> 战略结论：主攻**中国出海内容方**（跨境电商/课程出海/出海自媒体，zh→en 方向），现金流客群=外语→中文翻译创作者；
> 工程从"建设模式"切换到"卖货模式"——本方案是第一批真实付费用户前的工程收口清单。
> 运营动作（dogfooding 内容、淘宝/闲鱼接单）不属于工程 backlog，见 §4 项目主待办。

## 0. 范围原则

- **只做对"第一批付费用户"有直接贡献的单元**。Wave C/D 代码治理、uiloc W5（post-edit）、后端 server-emitted 中文（UI-BE-01）、USD 订阅续费等默认**暂缓**（§4.6 项目主可推翻）。
- 激活类改动一律**默认 inert**：代码合并 ≠ 生产开启；生产翻旗/部署由项目主执行。
- 付费 API 硬约束（CLAUDE.md）不变；CM-03 涉及 LLM 批量调用，**只能项目主显式触发**，脚本不得自动跑。

## 1. 执行流程约定（沿用 ship-unit）

- 每单元：从当前 origin/main 切干净 worktree（`D:\Claude\avt-worktrees\` 下）+ `claude/<feature>` 分支 → 测试先行 → 本地门绿 → 双层外审（多 lens 对抗 workflow + CodeX CLI）→ @codex bot PR 终审 → **全绿 squash-merge** → 同步本文档状态表。
- 子 agent 模型：Sonnet 5；钱路/红线单元的最终裁决不下放给子 agent。
- 提交只用显式 pathspec，永不 `git add .`。

## 2. 任务单元

### CM-01 topup 点数充值包购买链路（P0，钱路，规模 M）

**现状（2026-07-02 已核实）**：
- `gateway/pricing_schema.py:50-61` `TopupConfig(enabled=False)` / `TopupPackage`；`:259-276` 默认包 `topup_1000`=1000 点/¥39、`topup_3000`=3000 点/¥99。
- `gateway/billing.py` **全文件 0 处 topup**——`create_order`（:121）只收 `target_plan_code`，`_process_payment_event`（:1470）只处理套餐授点。整条购买链路缺失。
- credits 侧已预留：`gateway/models.py:635` ledger `source_label` 注释含 `"topup_1000"`；`credits_service.grant()` 支持 bucket_type+source_label；运行时 `bucket_priority` 已含 topup 桶位（trial→subscription→topup→free）。
- 前端 `/settings/billing` 的 CreditsSummary 只读展示 topup 桶余额，无购买入口。

**范围**：
1. 后端：`POST /api/billing/orders` 支持 topup SKU（实现期定形态，倾向最小侵入：与 `target_plan_code` 互斥的 `topup_code` 字段；不复制 create_order 骨架）。金额从 `TopupConfig` 运行时读；`enabled=False` 或 SKU inactive 时 fail-closed 拒单。
2. 结算：`_process_payment_event` 对 topup 订单授 topup bucket（`related_order_id` 关联；退款走既有 `revoke_buckets_for_order` 自然覆盖，需测试确认）。**结算幂等**：重复 webhook 不重复授点。
3. 三渠道：微信 Native / Paddle / PayPal。PayPal 需 topup 独立 USD 标价（仿 `price_usd_cents` 平行字段，后台可设）；**USD 价缺失时该渠道对 topup 隐藏（fail-closed，不做汇率换算）**。渠道之间不自动 fallback（红线）。
4. 前端：CreditsSummary 加"购买点数包"入口，复用 CheckoutCard 渠道选择；新 UI 串走 message key（zh/en 双语，过 cjk-guard）。定价页辅助入口为次要项，可裁。
5. 激活：`TopupConfig.enabled` 保持默认 False；合并后由项目主经 pricing admin 生产翻旗。

**验收**：无效/inactive SKU 400；enabled=False fail-closed；结算幂等测试；退款回收 topup 桶测试；未登录不可购买；套餐降级闸不受影响（topup 与 plan_rank 逻辑互不干扰）；uiloc 5 守卫绿；security-reviewer 强制过（支付路径）。

### CM-02 langpair part2 测试漂移修复（P1，规模 S）

**现状**：`tests/test_language_pairs_part2.py` 的 `test_unsupported_pair_returns_400_before_forward`（:234）与 `test_zh_en_not_allowed_returns_403_before_forward`（:248）因后续 express-clone consent gate 插入到 language gate 之前而失败。2026-07-02 调查判定为**测试维护漂移，非功能回归**。

**范围**：实现期先复现并确认根因。若确系断言过时→修测试（构造不触发 express gate 的请求或调整断言语义）；若发现 gate 顺序实为**行为变更**（语言闸被 consent 闸遮蔽对用户可见）→ 停下向项目主报告，升级为独立评审，**不得静默改生产 gate 顺序**。

**验收**：`test_language_pairs_part2.py` 全绿；生产代码零改动（或改动经独立评审）。

### CM-03 zh→en 上线前校准与人评材料包（P1，涉付费 API，规模 M）

**现状**：`natural_length_ratio=0.55` 标注 provisional，`RATIO_CALIBRATION_PENDING` 空 frozenset 让守卫形式通过、无实测数据；v3 方案（`docs/plans/2026-06-13-multilingual-mutual-translation-plan-v3.md`）自设的 pre-allowlist 人评门未跑；无真实 E2E 记录。

**范围**：
1. 校准脚本：对既有 fixture/历史任务 zh 源文本跑 zh→en 翻译、实测长度比分布，产出校准报告。**脚本本体不自动调 LLM——跑批由项目主显式触发，费用估算先行**。
2. 人评材料：从 v3 方案抽人评 rubric 成 checklist 文档 + 3-5 个样例任务生成清单（生成动作同样项目主触发）。
3. E2E 操作清单：一条生产真金 zh→en E2E 的 step-by-step（项目主执行）。

**验收**：校准报告落 `docs/`；ratio 常量更新有数据支撑（或有数据支撑的"维持 0.55"结论）；`RATIO_CALIBRATION_PENDING` 真实填充或守卫升级为实质断言。

### CM-04 support 客服组件英文化（P1，uiloc 补漏，规模 M）

**现状**：`frontend-next/src/components/support/` 9 个组件约 79 处内联 CJK，从未被任何 uiloc 单元认领（母 plan Task 3.3 枚举缺口）——EN 用户点开客服看到中文界面，是海外 GTM 前必补漏洞。

**范围**：按 uiloc W 切片同规格：新 namespace（如 `appSupport`）、R1 zh 逐字节一致、en 翻译、cjk-guard 基线只减不增。用户可见 6 个组件（SupportWidget/SupportLauncher/SupportConversationPanel/SupportMessageList/SupportHandoffBanner + 相关 lib）在范围内；admin 侧 3 个（AdminPresenceSwitcher/admin/*）按 uiloc 既有口径 out-of-scope，豁免并记录。同步在 `docs/plans/uiloc-tasks/UILOC-00-INDEX.md` 登记，堵枚举缺口。

**验收**：用户可见 support 组件 0 内联 CJK；zh 快照逐字节一致；5 个 uiloc CI 守卫绿。

### 执行顺序

**CM-01 → CM-02 → CM-04 → CM-03**（CM-02 极小可与 CM-01 评审等待期穿插；CM-03 的跑数据环节随时可被项目主触发提前）。

## 3. 状态表

| 单元 | 状态 | 分支/PR | 合并 commit | 备注 |
|---|---|---|---|---|
| CM-01 topup 购买链路 | ✅ 已合并+**已部署+已激活**（2026-07-02，项目主授权） | PR #94（基线 bump，74a5614b）+ PR #95 | 91e5ee3c | 外审：5-lens 对抗（P1+2×P2+P3 全修）+ CodeX CLI 两轮（P2 全修，第三轮限额→主模型终审兜底）+ @codex bot（3×P2 全修）；33 新测试 + 377 回归绿。生产：alembic 044 已应用、gateway/next 镜像重建换血（顺带上线已合并的 uiloc 工作台 EN + credits #77 修复）、`topup.enabled=true` 已翻（2026-07-02T04:32Z）。回滚物料：`backups/cm01-pre-20260702.tar.gz` + `gateway.pre-cm01/`/`frontend-next.pre-cm01/` 源目录 + `aivideotrans-gateway:pre-cm01`/`aivideotrans-next-backup:pre-cm01` 镜像 tag + `backups/pricing_runtime.pre-topup-20260702.json`；**hotfix PR #96（5ed531ac，2026-07-02 05:35Z 已上线）**：geo 交集空时回退 SKU 渠道（never-filter-to-zero），修复海外出口浏览器 topup 卡变砖 |
| CM-02 langpair 测试漂移 | ✅ 已合并（2026-07-02） | PR #97 | d716a9ea | 判定=测试维护漂移（express 默认 cosyvoice clone-only，consent 闸按已评审设计前置语言闸），零生产代码改动；2 测试路由 express→mimo 隔离 + 1 文档化测试钉闸序；10 个语言测试文件 215 全过；CodeX CLI 1×P3 与 @codex bot 1×P2 同点已修（before-forward 断言真实化）；闸序观察已上报 owner（如需语言闸前置另立单元） |
| CM-03 zh→en 校准+人评包 | ☐ 待开始 | | | 跑批需项目主触发 |
| CM-04 support 英文化 | ☐ 待开始 | | | 同步 UILOC INDEX |

## 4. 项目主待办（工程干不了的）

1. **真金终验**：PayPal 小额买+退各一单（验 webhook 闭环）、微信真钱一单——一直欠着，建议最先做。
2. **生产翻旗**（对应单元合并后）：`TopupConfig.enabled`（CM-01 后）；`language_pairs_enabled`+allowlist、`voice_catalog_target_language_filter_enabled`（CM-03 人评过后）。
3. **SEO 部署门**：/en 对爬虫开放，走 Via-154 部署（建议 CM-04 合并后）。
4. **运营双轮启动**：dogfooding demo 内容（每周 2-3 条，B站/小红书/抖音 + 英文版 YouTube Shorts/Reddit）+ 淘宝/闲鱼"视频翻译配音代做"服务单。
5. **CM-03 跑批显式触发 + 人评执行**（费用估算会先给出）。
6. **确认暂缓清单**：Wave C/D、uiloc W5、UI-BE-01 后端中文、USD 订阅续费——本方案默认暂缓，如有异议请指出。

## 4.5 CM-01 部署事故记录（2026-07-02 04:31–04:50Z，已闭环）

next 容器经 root compose 重建后拿到旧 env → next-intl proxy 自转发 `https://localhost:3000` TLS 错误 → 公网全站 500 约 18 分钟。根因=**M1 的 env 修复（`HOSTNAME=localhost`+`NODE_OPTIONS`）当年只落 app/docker-compose.yml、从未同步 root 入口**（feedback_apf_deploy_incident 的 Known Bad Pattern 反向复现）；部署冒烟只打了 127.0.0.1（307）探不出仅在公网转发头下显形的故障。修复：补丁 root compose（备份 `backups/docker-compose.pre-hostfix-20260702.yml`）→ 旧镜像验证公网 200 坐实根因 → CM-01 新镜像上线全 200；root vs 仓库 compose 已对齐零 diff。**新增部署铁律：①生产 recreate 前 diff root compose vs 仓库版对应服务段；②next 冒烟必须打公网 URL。**

## 5. 风险与红线备忘

- CM-01 是钱路：结算幂等、退款回收、fail-closed 缺价隐藏渠道；**支付渠道之间不自动 fallback**（CLAUDE.md 红线）。
- CM-03 LLM 批量调用是付费 API：脚本不得自动跑，只由项目主显式触发。
- 所有激活默认 inert；灰度节奏（尤其 zh→en allowlist 范围）由项目主定。
- 营销侧注意：面向"外语→中文创作者"的话术避开"搬运"定位，用"翻译工具/授权内容本地化"（平台政策风险）。
