# 免费/付费 TTS 路由：当前状态

> 本文档是短版状态说明。完整的历史调查记录见：
> `docs/archive/2026-03-30-free-vs-paid-tts-routing-status-and-recovery-plan.md`

## 产品不变量（不变）

| 不变量 | 描述 |
|--------|------|
| **INV-1** | 免费用户（`plan_code=free`, `service_mode=express`）必须走 CosyVoice |
| **INV-2** | 付费用户（`plan_code=plus/pro`, `service_mode=studio`）才走 Minimax |
| **INV-3** | Gateway 写入的 per-job `tts_provider` 优先级高于全局默认 |
| **INV-4** | pipeline 缺失 job identity 时不得静默降级到全局默认 |

## 代码链路已恢复（截至 2026-04-03）

历史调查文档（冻结于 2026-03-30）中描述的"Gateway → Pipeline job identity 传递链路断裂"**已在代码层修复**：

- `process_runner._build_command()` 已传递 `--job-id`
- `main.py` 已解析 `--job-id` 并填入 `ProcessConfig.job_id`
- `ProcessPipeline` 已根据 `job_id` 回读 job snapshot
- `TTSGenerator` 已优先使用 per-job provider

相关测试覆盖：
- `tests/test_tts_routing_invariants.py`（11 passed）
- `tests/test_tts_runtime_evidence.py`（11 passed）

## 当前剩余项

以下事项不需要"重新修代码链路"，而是运行时验证与部署态确认：

1. 生产环境 `admin_settings.json` 中 `tts_provider` 全局默认值仍为 `"minimax"`。当 per-job snapshot 可用后，此值只作为最后兜底。需确认生产部署后实际路由行为。
2. CosyVoice subprocess helper 在生产完整 pipeline 中的端到端验证。
3. 部署态容器内代码来源验证（`docker cp` + `docker restart` 或镜像重建）。
