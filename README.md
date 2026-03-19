# AIVideoTrans

Last updated: 2026-03-18

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

- Phase 1 重构已经收口
- canonical workflow/build 层已经存在
- `OutputDispatcher` 和 `manifest.json` 已落地
- `process` 仍然是最完整的 YouTube 兼容入口
- `publish` 当前只应表述为 minimal publish capability
- Web UI 已经进入“本地审校工作台”阶段，但不是当前主扩张方向
- 最新已验证基线：
  - `pytest -q` -> `474 passed, 2 warnings`
  - `python main.py --help` 能打印帮助，但当前仍经由 `SystemExit` 路径退出，退出码为 `1`

## 推荐阅读顺序

如果是第一次接手，建议按这个顺序读：

1. `CURRENT_PROJECT_STATUS.md`
2. `NEXT_EXECUTION_PRIORITY.md`
3. `PROCESS_WORKFLOW_CONVERGENCE.md`
4. `RUN_ENVIRONMENT.md`
5. `WEB_UI_STATUS.md`
6. `REFACTOR_PHASE1_SUMMARY.md`
7. `AIVideoTrans_Codex_执行版总文档_最终版.md`
8. `docs/archive/README.md`

说明：

- `README.md` 现在只保留项目总览、运行入口和当前边界
- 详细收敛路线放在 `PROCESS_WORKFLOW_CONVERGENCE.md`
- 当前唯一优先项放在 `NEXT_EXECUTION_PRIORITY.md`
- 历史演化细节与旧阶段说明放到 `docs/archive/`

## 如何运行

从仓库根目录运行。

### 查看 CLI

```bash
python main.py --help
```

### 启动 Web UI

```bash
python main.py web-ui
```

默认地址：

- [http://127.0.0.1:8876](http://127.0.0.1:8876)

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

## 相关文档角色

- `CURRENT_PROJECT_STATUS.md`
  - 当前事实快照
- `NEXT_EXECUTION_PRIORITY.md`
  - 下一轮唯一优先项
- `PROCESS_WORKFLOW_CONVERGENCE.md`
  - `process` 渐进接入新主线的详细路线
- `RUN_ENVIRONMENT.md`
  - 当前运行环境与依赖说明
- `WEB_UI_STATUS.md`
  - Web UI 当前完成度与暂停点
- `REFACTOR_PHASE1_SUMMARY.md`
  - Phase 1 收口总结
- `docs/archive/README.md`
  - 历史文档入口
