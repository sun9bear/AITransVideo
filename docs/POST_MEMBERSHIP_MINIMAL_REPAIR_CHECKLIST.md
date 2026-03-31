# 会员体系完工后：最小修复清单

> 前置条件：会员体系已完工、per-user plan 判定已稳定。
> 本清单只列最小闭环步骤，不展开做设计。

## 修复步骤

### 1. 恢复 `--job-id` 传递

**`src/services/jobs/process_runner.py` `_build_command()`**

在 command list 中加入：
```python
"--job-id",
job.job_id,
```

### 2. 恢复 `main.py` 接收 `--job-id`

**`main.py` `parse_process_args()`**

加入 argparse 参数：
```python
parser.add_argument("--job-id", default=None, help=argparse.SUPPRESS)
```

并传入 `ProcessConfig`：
```python
return ProcessConfig(..., job_id=parsed.job_id)
```

### 3. 确认 per-job `tts_provider` 优先级

验证 `tts_strategy.get_tts_provider_for_job()` 在收到有效 job record 时，
返回 record 上的 `tts_provider`，不回退到 `admin_settings.json`。

当前代码已正确实现（`tts_strategy.py` line 44-46），只需确认链路通了即可。

### 4. 免费用户端到端验证

提交一个 `service_mode=express` 的 job，确认：
- [ ] Gateway 写入 `tts_provider=cosyvoice`
- [ ] worker 命令行含 `--job-id`
- [ ] pipeline 日志输出 `[S4] TTS provider: cosyvoice`
- [ ] TTS 实际调用 CosyVoice（subprocess helper）
- [ ] Job 正常完成，无 hang
- [ ] 产物完整

### 5. 付费用户端到端验证

提交一个 `service_mode=studio` 的 job，确认：
- [ ] Gateway 写入 `tts_provider=minimax`
- [ ] pipeline 日志输出 `[S4] TTS provider: minimax`
- [ ] TTS 实际调用 Minimax
- [ ] Job 正常完成

### 6. 解除 xfail 测试

将 `tests/test_tts_routing_invariants.py` 中的两个 `xfail` 测试转为正式断言：
- `TestPipelineJobIdentityContract::test_build_command_includes_job_id`
- `TestMainAcceptsJobId::test_parse_process_args_has_job_id`

### 7. 容器内 runtime fingerprint + 完整 job 验证

部署后必须：
1. `docker cp` 新文件到容器（或重建镜像）
2. `docker restart aivideotrans-app`
3. runtime fingerprint 确认 `_build_command has --job-id: True`
4. 再提交验证 job

## 不在本清单范围内

- admin_settings.json 的处理策略（保留 / 废弃 / 改值）
- 进程池优化（subprocess helper 当前每次 fork 新进程，性能可接受）
- CosyVoice voice catalog 更新（`longyingjing_v3` 兼容性问题）
- DashScope SDK 线程生命周期根因定位（已被 subprocess 隔离绕过）
