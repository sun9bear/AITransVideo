# AIVideoTrans

Last updated: 2026-04-03

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

- 商业化 Phase 0-5 已放行（配额、Admin、支付基础设施）
- canonical workflow/build 层已落地：`ProjectBuilder` → `OutputDispatcher`
- `process` 仍是最完整的 YouTube 兼容壳，已通过 shared helpers 产出 `SemanticBlock`
- 前端主线为 `frontend-next/`（Next.js 16 + React 19）
- 详细快速入门见 [`docs/QUICKSTART.md`](docs/QUICKSTART.md)

## 推荐阅读顺序

新协作者请先阅读 [`docs/QUICKSTART.md`](docs/QUICKSTART.md)，其中包含完整的推荐阅读顺序和历史文档边界说明。

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

> 注：Web UI (8876) 已在 Phase 4 废弃，所有功能已迁移到上述服务。

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

### 1. `workflow` 是未来主线

- `ProjectWorkflow.run_build()` 是 canonical pre-output path
- workflow demo 已经走 `run_build() -> OutputDispatcher`

### 2. `process` 仍然保留，但要渐进收编

- `process` 现在还是最完整的兼容壳
- 但它不应继续演化成第二套架构中心
- 下一步重点是让它逐步消费 `run_build() + OutputDispatcher`

### 3. `publish` 目前只承诺最小能力

- 当前可产出 `dubbed_video.mp4`
- 前提是存在 source video artifact
- 字幕烧录、原音混合、更丰富发布控制都还未完成

### 4. Web UI 先稳住，不先扩张

- `转录与发言人`、`翻译与重写` 审校页已经可用
- 下一轮大工作不应继续铺更多 UI 能力
- 先做 `process -> workflow/output` convergence 更重要

## 相关文档

- [`docs/QUICKSTART.md`](docs/QUICKSTART.md) — 新协作者第一入口
- `CLAUDE.md` — Claude Code 协作约束
- `AGENTS.md` — 架构规则与 sprint 约束
- `docs/acceptance/` — 各阶段验收记录
- `docs/archive/` — 历史文档
