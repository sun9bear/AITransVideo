# AIVideoTrans

Last updated: 2026-05-24

## 项目是什么

`AIVideoTrans` 是一个面向视频与音频本地化的 Python 工作流项目。

当前仓库的重点不是“一键最终成片”，而是先把最容易出错、最耗时的链路自动化，并产出可继续人工审校和编辑的工程化结果。

当前主线可以概括为：

- 媒体理解 / transcript attribution
- 忠实翻译
- 面向配音的重写
- semantic chunking
- TTS
- alignment
- editor / minimal publish 输出

## 当前状态

- Gateway 是套餐、试用、定价、权益、支付策略和生产安全的事实源。
- 当前任务模式为 `express` / `studio` / `smart`；智能版仍在实施推进中，创建入口由 `AVT_ENABLE_SMART_MODE` 与 admin `smart_mode_enabled` 双层开关控制。
- Workflow 主线保持 `SemanticBlock -> TTS -> DSP-first alignment -> cue_pipeline -> editor outputs`，主交付目标仍是剪映草稿工程。
- `process` 仍是最完整的 YouTube 兼容壳，但输出和交付持续向 shared workflow/output 结构收敛。
- 前端主线为 `frontend-next/`（Next.js 16 + React 19）。
- Smart analytics、Phase 1a/1b report analysis、CSRF write-route guard、R2/Pan 运维面都已进入当前架构事实。
- 详细快速入门见 [`docs/QUICKSTART.md`](docs/QUICKSTART.md)

## 推荐阅读顺序

新协作者先读 [`docs/QUICKSTART.md`](docs/QUICKSTART.md)，再读 [`docs/graphs/GITNEXUS_PROJECT_GRAPH.md`](docs/graphs/GITNEXUS_PROJECT_GRAPH.md) 和 [`docs/plans/README.md`](docs/plans/README.md)。

## 安装依赖

根目录 Python 依赖声明在 `pyproject.toml` 中。

```bash
# 安装运行时依赖 + 开发依赖
pip install -e ".[dev]"

# 或只安装运行时依赖
pip install -e .
```

> `gateway/` 的依赖独立管理，见 `gateway/requirements.txt`。

## 如何运行

从仓库根目录运行。

### 查看 CLI

```bash
python main.py --help
```

### 启动服务（Docker Compose 部署）

当前架构已迁移到 Gateway + Job API + Next.js 前端：

| 服务 | 端口 | 说明 |
|------|------|------|
| Gateway | 8880 | 统一入口：认证、路由、代理 |
| Job API | 8877 | 任务 CRUD、状态、日志、产物 |
| Next.js | 3000 | 前端页面 |

> 注：Web UI (8876) 已在 Phase 4 废弃，`src/services/web_ui/server.py` 和 `handler.py` 在 2026-04-17 legacy migration cleanup 中彻底删除（见 `docs/plans/2026-04-17-legacy-migration-cleanup.md`）。HTTP 产品面已迁移到 Gateway、Job API 和 Next.js；`src/services/web_ui/` 中剩余的是被 Job API 复用的库模块。

### 启动 control panel

```bash
python main.py control-panel
```

默认地址：

- [http://127.0.0.1:8765](http://127.0.0.1:8765)

### 运行测试

```bash
python -m pytest -q
```

### Legacy YouTube 主入口

```bash
python main.py process <youtube_url>
```

### Workflow-oriented 本地 demo

```bash
python main.py local-audio-demo <local_audio_path> [translation_mode] [tts_mode] [--output editor|publish|both]
```

```bash
python main.py local-video-demo <local_video_path> [translation_mode] [tts_mode] [--output editor|publish|both]
```

更完整的环境、依赖、`ffmpeg` / `yt-dlp` 说明见 `RUN_ENVIRONMENT.md`。

## 当前最重要边界

### 1. Gateway 是商业事实源

- 套餐、试用、价格、权益、Smart 可用性和支付策略都由 Gateway 管理。
- 前端只能消费 Gateway facts，不能复制一套价格或权益判断。

### 2. `SemanticBlock` 和 DSP-first 是工作流主线

- TTS unit 保持为 `SemanticBlock`，不是 subtitle line。
- 对齐策略保持 DSP first，rewrite loop 是兜底。
- 字幕重定时保持数学/确定性逻辑，不交给 LLM。

### 3. `process` 仍然保留，但要渐进收编

- `process` 现在还是最完整的兼容壳
- 但它不应继续演化成第二套架构中心
- 下一步重点是让它逐步消费 `run_build() + OutputDispatcher`

### 4. 智能版正在实施

- Smart P0/P1 已完成，P2 launch blockers 已闭环，但当前仍按“正在实施中”管理。
- 当前后续重点是 Smart analytics 指标、possible-match auto-reuse 后的质量监控、文档漂移清理和 shadow verifier 试点。

### 5. 交付目标是剪映草稿

- Jianying draft 是主交付面，直接 rendered MP4 不是主路径。
- R2、materials pack、Pan backup 属于交付和运维配套面。

## 相关文档

- [`docs/QUICKSTART.md`](docs/QUICKSTART.md) — 新协作者第一入口
- [`docs/graphs/README.md`](docs/graphs/README.md) — 当前图谱入口
- [`docs/plans/README.md`](docs/plans/README.md) — 当前方案状态索引
- `CLAUDE.md` / `AGENTS.md` — 协作约束、架构规则与 review 边界
- `docs/archive/` — 历史文档，只作溯源
