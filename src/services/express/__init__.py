"""Express (快捷版) CosyVoice auto-clone orchestration (Phase 4.3a PR2-E).

模块边界（spec §6.1 + Codex PR2-E）：

- **不 import gateway**（D.7）：``reservation_client`` 走 HTTP 调 gateway
  internal endpoints，不直接 import gateway service。
- **不 import boto3**（NG5）：sample 上传走 PR1-E1 gateway endpoint（F 接），
  不在 app 容器直连 OSS。
- **不调 MiniMax**：任何失败回 CosyVoice 预设音色，不 fallback 到付费 MiniMax。
- ``audit`` 只写本地 sidecar JSONL + runtime log，不打外部 API。

PR2-E 只交付编排模块 + 调用顺序 / 失败路径守卫；process.py 主流程接入是
PR2-F。``auto_clone.run_express_auto_clone`` 用**注入式 client**（DI），把
upload / worker clone / register-smart 的具体实现留给 F 装配（F 用真
``reservation_client`` / ``MainlandWorkerClient`` / PR1-E1 upload / register-smart）。
"""
from __future__ import annotations

from services.express.main_speaker import identify_express_main_speaker

__all__ = ["identify_express_main_speaker"]
