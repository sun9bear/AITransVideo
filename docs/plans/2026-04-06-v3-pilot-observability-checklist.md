# V3 试运行观测指标清单

> 状态：试运行观测清单  
> 时间：2026-04-06  
> 适用阶段：V3 点数体系方案试运行前 / 试运行中 / 第一轮校准前  
> 目标：为 V3 第一轮成本校准、点数锚点校准、Top-up 定价校准、Plus/Pro 附赠点数校准提供最小而充分的数据基础

---

## 1. 使用方式

本清单不是新的产品方案，而是 V3 试运行期需要持续回收的观测项。

建议执行节奏：

1. 先按当前 V3 草案上线或灰度试运行
2. 连续采集 `2-4 周`
3. 按本清单汇总一次试运行数据
4. 再决定是否调整：
   - `K_cn_chars_per_src_min`
   - 各 TTS 模型单价假设
   - `1 点成本承载`
   - `1 点售价`
   - `Free / Trial / Plus / Pro / Top-up` 点数口径

---

## 2. 优先级

### P0：没有这些就无法做第一轮校准

- 实际 `K 值`
- 实际 TTS 单价
- 翻译 / S2 / 重写 / 服务器的真实每分钟成本
- 快捷版 / 工作台版使用占比

### P1：有了这些才能优化扣点和毛利结构

- 各质量档位使用占比
- 重写触发率
- 失败返还率
- 用户的 Top-up 与订阅使用关系

### P2：有助于第二轮优化，但不阻塞第一轮定价校准

- 用户任务长度分布
- 不同 TTS 供应商的失败率 / 回退率
- Trial 期间的转化路径

---

## 3. 核心观测指标

## 3.1 字符与分钟换算

### 指标

- `source_video_minutes_total`
- `final_cn_chars_total`
- `K_cn_chars_per_src_min_actual`

### 计算方式

```text
K_cn_chars_per_src_min_actual
= final_cn_chars_total / source_video_minutes_total
```

### 目标

验证当前方案中的：

```text
K_cn_chars_per_src_min = 250
```

是否合理。

### 建议输出

- 总体平均值
- P50 / P75 / P90
- 按 `快捷版 / 工作台版` 分组
- 按视频类型分组（如口播 / 教学 / 访谈，如后续有能力识别）

### 影响

该值会直接影响：

- TTS 成本估算
- 点数成本锚点
- 各模式毛利率判断

---

## 3.2 实际 TTS 成本

### 指标

- `tts_provider`
- `tts_model`
- `billed_chars`
- `tts_cost_rmb_total`
- `tts_cost_rmb_per_10k_chars_actual`
- `tts_cost_rmb_per_src_min_actual`

### 目标

验证和校准当前文档中的这些假设：

- 豆包保守口径
- MiniMax 按量口径
- CosyVoice 国内 / 海外口径

### 建议输出

- 按模型汇总实际账单
- 按供应商汇总实际账单
- 按采购方式区分：
  - 按量
  - 资源包
  - 包年 / 包月折算

### 影响

这项会直接决定：

- 快捷版是否继续绑定当前低成本 TTS 池
- 高质 / 旗舰档是否需要加价
- `1 点成本承载` 是否需要重算

---

## 3.3 翻译 / S2 / 重写真实 LLM 成本

### 指标

- `translation_input_tokens_total`
- `translation_output_tokens_total`
- `translation_cost_rmb_total`
- `s2_input_tokens_total`
- `s2_output_tokens_total`
- `s2_cost_rmb_total`
- `rewrite_input_tokens_total`
- `rewrite_output_tokens_total`
- `rewrite_cost_rmb_total`

### 衍生指标

- `translation_cost_rmb_per_src_min`
- `s2_cost_rmb_per_src_min`
- `rewrite_cost_rmb_per_src_min`

### 目标

替换当前 V3 文档里的固定摊销占位值：

- `C_translate = 0.03`
- `C_s2_review = 0.02`
- `C_rewrite = 0.02`

### 说明

当前这三项在文档中属于 **安全冗余估算**，试运行后应尽量用真实账单回填。

---

## 3.4 重写触发率

### 指标

- `jobs_total`
- `jobs_with_rewrite`
- `rewrite_trigger_rate`

### 计算方式

```text
rewrite_trigger_rate
= jobs_with_rewrite / jobs_total
```

### 目标

判断重写成本是否应该继续按“固定摊销”处理，还是改成：

```text
重写发生概率 × 单次平均成本
```

### 影响

这项会影响：

- 工作台版基础档扣点是否偏高
- Trial / Plus / Pro 毛利率是否被高估或低估

---

## 3.5 服务器每分钟成本

### 指标

- `infra_cost_rmb_total`
- `infra_cost_rmb_per_day`
- `billable_src_minutes_total`
- `infra_cost_rmb_per_src_min`

### 说明

这里的服务器成本建议至少覆盖：

- 应用服务器
- 任务处理实例
- GPU / 高性能节点（如果有）
- 存储与网络

### 目标

验证当前文档中的：

```text
C_server = 0.03 元/分钟
```

是否过高或过低。

---

## 4. 产品与流量结构指标

## 4.1 使用模式分布

### 指标

- `jobs_express_count`
- `jobs_studio_count`
- `minutes_express_total`
- `minutes_studio_total`
- `express_share`
- `studio_share`

### 目标

判断：

- `快捷版 10 点/分钟`
- `工作台基础版 15 点/分钟`

这两个主扣点锚点是否合理。

### 影响

如果工作台版占比远高于预期，则：

- Trial / Plus / Pro 点数可能需要下调
- 或工作台版基础档扣点需要上调

---

## 4.2 质量档位分布

### 指标

- `quality_standard_share`
- `quality_high_share`
- `quality_flagship_share`

### 目标

确认高质 / 旗舰是否真的是少数高客单场景，还是成为主流路径。

### 影响

如果高价档位占比高于预期：

- 当前点数锚点可能偏低
- Pro 的附赠点数需要更谨慎

---

## 4.3 任务时长分布

### 指标

- `job_minutes_p50`
- `job_minutes_p75`
- `job_minutes_p90`
- `job_minutes_p95`

### 目标

判断：

- Free 500 点是否足够形成体验但不够长期白嫖
- Trial 300 点 / 7 天是否足够体验工作台核心价值

---

## 5. 账本与回滚健康度指标

## 5.1 预扣 / 结算 / 返还一致性

### 指标

- `reserve_count`
- `capture_count`
- `release_count`
- `failed_job_refund_count`
- `ledger_inconsistency_count`

### 目标

验证：

- reserve -> capture -> release

链路是否完整。

### 必查问题

- 是否存在只 reserve 不 capture/release 的悬挂记录
- 是否存在 capture 超过 reserve 的异常
- 是否存在失败任务未返还

---

## 5.2 Bucket 消耗正确性

### 指标

- `free_bucket_debit_total`
- `trial_bucket_debit_total`
- `subscription_bucket_debit_total`
- `topup_bucket_debit_total`

### 目标

验证当前规则是否真的被执行：

- 快捷版优先用 `Free`
- 工作台版优先用 `Trial`
- `Subscription` 优先于 `Top-up`

### 影响

如果 bucket 选择经常与预期不符，说明：

- 规则写错
- 或 ledger enforcement 不够强

---

## 5.3 退款与回滚健康度

### 指标

- `refund_count`
- `refund_with_credit_rollback_count`
- `refund_with_entitlement_recompute_count`
- `refund_negative_balance_count`

### 目标

验证退款后：

- billing truth
- credits ledger
- effective entitlements

是否真的保持一致。

---

## 6. 商业转化指标

## 6.1 Trial 转化

### 指标

- `trial_granted_users`
- `trial_activated_users`
- `trial_to_paid_conversion_rate`
- `trial_expired_unused_rate`

### 目标

判断：

- `Trial = 300 点 / 7 天`

是否过多、过少、或刚好够体验工作台价值。

---

## 6.2 Top-up 与订阅关系

### 指标

- `topup_purchase_rate_free_users`
- `topup_purchase_rate_subscribed_users`
- `topup_attach_rate_plus`
- `topup_attach_rate_pro`

### 目标

判断当前 Top-up 包价格是否：

- 对免费用户有吸引力
- 又不会打穿订阅价值

---

## 6.3 点数消耗与订阅消耗率

### 指标

- `plus_credits_consumption_rate`
- `pro_credits_consumption_rate`
- `plus_exhaustion_rate`
- `pro_exhaustion_rate`

### 目标

判断：

- `Plus = 3500 点 / 月`
- `Pro = 12000 点 / 月`

是否偏高或偏低。

### 判断参考

- 如果大量用户长期只用掉 `20% 以下`，附赠点数可能偏多
- 如果大量用户很快用完并强依赖 Top-up，附赠点数可能偏少

---

## 7. 第一轮校准时必须回答的问题

试运行 `2-4 周` 后，至少应能回答以下问题：

1. `K_cn_chars_per_src_min = 250` 是否准确？
2. 当前实际最低成本 TTS 池到底是哪组模型？
3. `1 点 ≈ 0.015 元成本承载` 是否还成立？
4. `快捷版 10 点/分钟` 是否仍有合理毛利？
5. `工作台基础版 15 点/分钟` 是否需要调整？
6. `Plus 3500` / `Pro 12000` 是否过高或过低？
7. Top-up 方案 A 是否会冲击订阅，或是否定价过高影响转化？

---

## 8. 当前建议的最小落地要求

在真正进入 V3 开发前，至少应保证未来实现可以采集到：

- 每任务真实源视频分钟
- 每任务最终中文字符数
- 每任务 TTS 提供商 / 模型 / 字符数 / 成本
- 每任务翻译 / S2 / 重写 token 与成本
- 每任务 reserve / capture / release 账本链路
- 每任务实际命中的 bucket 类型

如果这些观测点采不上来，V3 的第一轮调价就只能继续靠经验估算。

---

## 9. 当前结论

V3 试运行期最重要的不是立刻把所有点数规则做复杂，而是：

- 先把数据采对
- 先把成本看清
- 先把用户真实使用行为看清

只有这样，后续对：

- 点数锚点
- Top-up 定价
- Plus / Pro 附赠点数
- 快捷版 / 工作台版扣点

的调整才会是“基于事实的校准”，而不是新的拍脑袋定价。

