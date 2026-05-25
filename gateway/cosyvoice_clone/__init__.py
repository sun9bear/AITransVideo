"""Gateway 端 CosyVoice clone 业务接线（Phase 4.1）。

子模块（按 Phase 4.1 子任务对应）：

- ``sample_validator`` — Phase 4.1 B：音频样本 5 维硬校验
  （格式 / 时长 / 大小 / 采样率 / 内容质量提示）
- ``api`` — Phase 4.1 C：``POST /api/voice/cosyvoice/clone`` endpoint
  （allowlist gate / modal_version v1 / consent / 单 target_model）

设计约束：

- 本包属于 **Gateway 侧 CosyVoice clone 业务接线层**，与
  ``src/services/mainland_worker/`` 形成"国内 worker 端"与"美国 Gateway 端"
  分离。
- 业务层不直接 ``import dashscope`` —— 所有真实 provider 调用都通过
  ``MainlandWorkerClient`` 转发到武汉 worker。
- 付费 API 硬约束（CLAUDE.md）：clone 必须用户显式触发，本包任何
  函数都不在 fallback / scheduled job / 自动 retry 路径调 worker。
"""
