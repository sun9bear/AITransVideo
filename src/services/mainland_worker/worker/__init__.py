"""Mainland Voice Worker — server 端实现。

部署目标：武汉 ECS（``/opt/aivideotrans-mainland-worker/current``）。
本子包是 FastAPI app + provider 抽象，**Phase 1 默认挂载 mock provider**，
不 import ``dashscope``，不访问公网。

包结构：

- ``app``       — FastAPI app 工厂 + middleware 装配
- ``config``    — 从 env 读取的运行时配置
- ``audit``     — JSONL audit logger（plan §审计日志）
- ``providers`` — Provider 协议 + Mock / Real 实现

Phase 4 真实接入 DashScope 时新增 ``providers/real_cosyvoice.py``；
本子包除增加该文件外不应改动。
"""
