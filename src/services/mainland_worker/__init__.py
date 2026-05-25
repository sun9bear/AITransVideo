"""Mainland Voice Worker — 国内 voice clone / TTS 中转 worker。

部署架构（详见 ``docs/plans/2026-05-24-cosyvoice-domestic-worker-plan.md``）::

    US Job API  ──HMAC──>  武汉 Nginx  ──>  Worker (这个包的 ``worker`` 子包)
                                                │
                                                └──>  DashScope CosyVoice (mainland)

Phase 1 阶段所有 provider 调用都是 mock：worker handler 返回 deterministic
fake voice id 和 silent WAV，不 import ``dashscope``，不打公网。

包结构：

- ``types``       — 请求/响应 dataclass（client 与 worker 共享）
- ``hmac_auth``   — HMAC-SHA256 签名 + 验证 + Key 轮换（client 与 worker 共享）
- ``dispatch``    — voice metadata → 是否走 worker 的决策（US 主机用）
- ``silent_wav``  — 生成 silent WAV bytes 的纯函数（worker mock provider 用）
- ``client``      — US 主机调用 worker 的 HTTP client（httpx-based）
- ``worker``      — FastAPI server 端（部署在武汉 ECS 上）

设计约束：

1. **整个包不 import dashscope / 真实付费 API SDK**。Phase 4 真实接入时，
   通过 ``worker.providers.RealCosyvoiceProvider`` 注入 — Phase 1 默认
   挂载的是 ``worker.providers.MockCosyvoiceProvider``。
2. **不 import ``services.jobs`` / ``services.gemini`` / ``services.tts``** —
   worker 是独立部署组件，不能反向依赖主 pipeline。Phase 1 守卫测试 AST 扫。
3. **付费 API 硬约束**（CLAUDE.md）：clone 最多 1 次 provider call、
   单段 TTS 最多 3 次、batch 最多重提 1 次。worker 端的 mock provider
   不重试（因为不会失败），client 端的 retry 上限由 ``client.MainlandWorkerClient``
   统一收口。
"""
