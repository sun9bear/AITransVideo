# 免费/付费 TTS 路由：当前状态 & 恢复计划

> 冻结于 2026-03-30。会员体系完工前不做路由修复，但必须在完工后按本文清单验收。

## 1. 产品不变量

| 不变量 | 描述 |
|--------|------|
| **INV-1** | 免费用户（`plan_code=free`, `service_mode=express`）**必须走 CosyVoice** |
| **INV-2** | 付费用户（`plan_code=plus/pro`, `service_mode=studio`）**才走 Minimax** |
| **INV-3** | Gateway 写入的 per-job `tts_provider` 优先级必须高于 `admin_settings.json` 全局默认 |
| **INV-4** | pipeline 缺失 job identity 时不得静默降级到全局默认 provider |

## 2. 当前系统状态

### ✅ Gateway 判定正确

`gateway/job_intercept.py` `compute_job_policy()` 按 `service_mode` 正确分流：
- `express` → `tts_provider: cosyvoice`
- `studio` → `tts_provider: minimax`

已有测试覆盖：`tests/test_gateway_job_policy.py::TestComputeJobPolicy`

### ❌ Gateway → Pipeline 的 job identity 传递链路断裂

```
Gateway 写入 job record: tts_provider=cosyvoice
     ↓
process_runner._build_command(): 不传 --job-id
     ↓
worker main.py: 无 --job-id 参数定义
     ↓
ProcessConfig.job_id = None
     ↓
pipeline 无法加载 job record
     ↓
tts_generator 走 legacy fallback
     ↓
admin_settings.json → "tts_provider": "minimax"
     ↓
免费用户实际走了 Minimax ❌
```

### ❌ admin_settings.json 全局默认值

生产服务器 `/opt/aivideotrans/config/admin_settings.json` 设置 `"tts_provider": "minimax"`。
这是 legacy 时代的配置。当 per-job snapshot 可用后，此值应只作为最后兜底。
当前因链路断裂，它成了实际生效的唯一值。

## 3. Round 7 结论边界

### Round 7 已证明

1. subprocess helper 机制本身有效（TTS 子进程隔离，worker 正常退出，无 hang）
2. `docker cp` + `docker restart` 部署流程可使容器内代码生效
3. **在 Minimax 路径下**，完整 job 能正常完成

### Round 7 未证明

1. ❌ 免费用户 CosyVoice 路径已打通
2. ❌ 免费/付费分流正确
3. ❌ CosyVoice subprocess helper 在完整 pipeline 中不 hang（从未被真正调用）
4. ❌ per-job `tts_provider` 能被 pipeline 消费

## 4. 当前不建议立即修的原因

1. 会员体系仍在施工中（另一个会话负责）
2. `--job-id` 传递链路与会员体系的 job record schema 直接耦合
3. 现在修传递链路，会员体系完工后可能需要再改
4. 更适合的策略：冻结结论、补保护测试、等会员体系完工后一次性收口

## 5. 会员体系完工后的最小修复目标

见独立文件 `POST_MEMBERSHIP_MINIMAL_REPAIR_CHECKLIST.md`。

## 6. 部署注意事项

### 容器代码来源

```
/opt/aivideotrans/app/ 在容器内不是 bind mount
↓
主机修改文件 → 容器不可见
↓
必须 docker cp + docker restart 或重建镜像
```

### 运行态验证规则

做任何应用层结论之前，必须先验证：
1. 容器内文件 sha256 是否与预期一致
2. runtime fingerprint（`inspect.getsource`）是否反映新代码
3. 不以主机文件状态代替容器运行态
