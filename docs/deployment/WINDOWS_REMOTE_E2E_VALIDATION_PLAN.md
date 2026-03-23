# WINDOWS_REMOTE_E2E_VALIDATION_PLAN

Last updated: 2026-03-19

## Current Status Note

This validation plan has been executed for the current phase and the acceptance scope has passed.
It should now be treated as the accepted Windows stable-baseline validation reference, not as an open implementation plan.

## 目标机器前置条件

- Windows 主机
- 已安装 Python，当前已验证基线为 `Python 3.12`
- 仓库代码已放到固定目录，且该目录可长期读写
- 运行依赖已手动安装完成
- `ffmpeg` 已在 `PATH`
- YouTube 路径所需依赖可用，例如 `yt-dlp`
- 真实 provider 所需配置已在环境变量或 `autodub.local.json` 中准备好
- 主机具备稳定外网访问能力

## 部署后验收前检查项

- `powershell -ExecutionPolicy Bypass -File scripts/start_remote_workbench.ps1 -Service all` 如遇 `127.0.0.1:8876` 已被旧 `python main.py web-ui` 占用，必须 fail fast，而不是继续把公网入口接到错误模式
- `Web UI` 只监听 `127.0.0.1:8876`
- `Job API` 只监听 `127.0.0.1:8877`
- `control-panel` 如启用，也只监听 `127.0.0.1:8765`
- 公网入口可通过 HTTPS 访问，并且先经过认证
- 不能从公网直接访问 `8876`、`8877`、`8765`
- 仓库下 `jobs/`、`projects/` 可正常写入

## 一条真实任务的端到端验收步骤

建议优先选择一个已知较稳定、且最好能触发 review gate 的真实 YouTube URL。

1. 启动本机 `Job API`、`Web UI` 和公网入口组件。
2. 从公网地址访问 Web UI，先验证认证闸门存在。
3. 通过 Web UI 提交一条真实 `youtube_url` 任务。
4. 确认页面能看到：
   - `queued` / `running` 状态变化
   - 实时日志或阶段消息
   - 当前任务的基础摘要
5. 等待任务进入：
   - `waiting_for_review`，或
   - 直接成功结束
6. 如果进入 review gate，则在 Web UI 中完成对应审批并执行 continue。
7. 任务结束后，验证结果摘要与关键产物下载能力。

## review continue 验收点

- 页面能明确显示任务进入 `waiting_for_review`
- 页面能看到当前 `review_gate` 或等价的待确认状态
- 触发 review 时，`127.0.0.1:8876` 当前监听者必须是 `run_remote_workbench_service.py web-ui`，不能是旧 `main.py web-ui`
- 执行 continue 后，任务应以同一个 `job_id` 恢复，而不是生成新任务
- continue 后状态应从 `waiting_for_review` 回到 `running` 或最终终态
- 如果本地检查文件，`review_state.json` 的审批语义应与现有仓库保持一致

## 结果摘要 / 下载验收点

- `/api/state` 不得返回 raw provider API key；只允许返回 provider 名称、模型信息、环境变量名、是否已配置之类安全展示字段
- 结果摘要能显示：
  - `job_id`
  - `status`
  - `manifest_path`
  - `review_gate`
  - `error_summary` 或 `fallback_summary`（如存在）
  - manifest-derived outputs / artifacts 摘要
- 下载面只出现白名单关键对象，不出现目录浏览器
- 白名单关键对象当前只包括：
  - `manifest.json`
  - `translation.segments`
  - `editor.subtitles`
  - `editor.dubbed_audio_complete`
  - `publish.dubbed_video`
- 关键产物下载应与 manifest 中的 stable key 对应
- manifest 缺失或不完整时，应表现为“摘要可容错、对应下载不可用”，而不是回退到任意路径读取

## 失败时记录什么信息

- 验收时间和目标机器信息
- 公网访问 URL
- 失败阶段：
  - 认证
  - 提交
  - 运行
  - review continue
  - 结果摘要
  - 关键产物下载
- `job_id`
- `jobs/<job_id>.json`
- `jobs/<job_id>.events.jsonl`
- `project_dir`
- `manifest_path`
- 如有 review，记录对应 `review_state.json`
- 部署级运行日志和公网入口日志

## 验收通过标准

- 只能通过 HTTPS + 认证访问公网 Web UI
- 不能从公网直接访问应用本机端口
- 一条真实 YouTube 任务可以从公网 Web UI 提交并观察执行
- 若任务触发 review gate，continue 能正常恢复同一任务
- 最终能看到 manifest-derived 结果摘要
- 最终能下载白名单内的关键产物
- 全流程不需要把 Job API 当作公网接口直接操作
