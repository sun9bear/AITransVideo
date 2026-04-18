# WEB_CONSOLE_RUNTIME_CONFIG_CONTRACT.md

> 说明：环境变量必须写入实际持久配置链路（如 `/opt/aivideotrans/config/.env`、`autodub.local.json`、`remote_workbench.local.json`），并经 `systemctl restart aivideotrans-compose` 触发 compose 重建后才算真正生效；只存在于临时 shell 环境不算完成配置。

## 0. MiniMax TTS 参数职责分层（当前阶段）

### 持久运行配置项

- `AUTODUB_TTS_API_KEY`
- `AUTODUB_TTS_BASE_URL`
- `AUTODUB_TTS_MODEL_NAME`，当前默认固定为 `speech-2.8-turbo`
- `AUTODUB_TTS_API_PROTOCOL=minimax_t2a_v2`
- `AUTODUB_TTS_TTS_PROVIDER=minimax_tts` 与 `AUTODUB_TTS_PLATFORM=minimax_domestic`，或等价本地配置；当前建议固定到 MiniMax 国内版语义，减少音色库匹配歧义
- `AUTODUB_TTS_VOICE_REGISTRY_PATH` 或等价本地配置；当链路依赖音色库匹配时由运行配置提供

### 任务级参数

- `voice_id`
- 当前新建翻译页如承载任务级音色输入，应沿用现有 `Voice A / Voice B` 语义来映射 `speaker_a / speaker_b` 的 `voice_id`
- 当前阶段不在新建翻译页暴露 `base_url` 或 `model_name`；这两项属于后续设置页职责

### 后端自动决策项

- 任务未显式提供 `voice_id` 时，继续按现有后端真实逻辑处理
- 优先查找 voice registry
- 未命中时再进入自动克隆 / review 链路
- 新前端不伪造新的 fallback，只负责把“未设置 `voice_id`”如实传给现有后端语义

## 1. 当前单用户运行的必要配置项

| 配置项 | 当前是否必需 | 提供方式 | 备注 |
| --- | --- | --- | --- |
| `autodub.local.json` 可读取且 JSON 合法 | 必需 | 本地配置文件 | 文件可不存在；但一旦存在，必须可正常解析 |
| `remote_workbench.local.json` 可读取且绑定合法 | Linux / 公网部署必需 | 本地配置文件 | 决定 `web-ui`、`job-api`、`public-entry` 绑定 |
| `ASSEMBLYAI_API_KEY` 或 `autodub.local.json > assemblyai.api_key` | 必需 | 环境变量或本地配置 | 缺失会阻断真实转录链路 |
| 当前翻译路由至少有一个可用 provider key | 必需 | 环境变量或本地配置 | 默认优先是 Gemini；可回退到 DeepSeek / OpenAI / Anthropic |
| `AUTODUB_TTS_API_KEY` / `AUTODUB_TTS_BASE_URL` / `AUTODUB_TTS_MODEL_NAME` / `AUTODUB_TTS_API_PROTOCOL` | 必需 | 环境变量或本地配置 | 当前 MiniMax 国内版真实链路至少需要这些系统级参数；默认模型固定为 `speech-2.8-turbo` |
| `AUTODUB_TTS_TTS_PROVIDER` / `AUTODUB_TTS_PLATFORM` | 条件必需 | 环境变量或本地配置 | 当前建议固定为 `minimax_tts` / `minimax_domestic`；当依赖音色库匹配时可减少 provider / platform 歧义 |
| 任务级 `voice_id`（当前前端承载为 `voice_a` / `voice_b`）或可用 voice registry path | 条件必需 | 任务参数或本地配置 | `voice_id` 属于任务级参数；若未提供，应由后端继续走音色库匹配 / 自动克隆 |
| `AUTODUB_TTS_CLONE_*` 或与 TTS 共用的 clone 配置 | 条件必需 | 环境变量或本地配置 | 当前真实自动 clone 链路需要时才构成阻断 |
| `youtube.cookie_file` 或 `youtube.cookies_from_browser` | 条件必需 | 本地配置文件 | 不是所有视频都需要，但受限视频会真实阻断下载 |
| `ffmpeg` 在 `PATH` 中可用 | 必需 | 运行环境 | 缺失会阻断下载后抽音与媒体处理 |
| `/job-api/* -> 8877` | Linux / 公网部署必需 | 反向代理配置 | 前端读取任务和详情必须可达 |
| `/api/* -> 8880` (Gateway) | Linux / 公网部署必需 | 反向代理配置 | Gateway 原生端点（admin 等）必须可达 |
| Gateway (8880) 作为统一入口 | Linux / 公网部署必需 | 反向代理配置 | 所有 API 流量经由 Gateway 认证和路由 |
| `AUTODUB_PUBLIC_ENTRY_USERNAME` / `AUTODUB_PUBLIC_ENTRY_PASSWORD_HASH` | 公网入口必需 | 环境变量 | 只影响公网 Basic Auth 入口，不影响本地 localhost 运行 |

## 2. 每项配置缺失时，用户最应该看到什么提示

| 缺失项 | 最应该看到的提示 |
| --- | --- |
| `autodub.local.json` 格式错误 | “本地运行配置文件格式错误，请修复 `autodub.local.json` 后再继续。” |
| `remote_workbench.local.json` 缺失或非法 | “运行时绑定配置无效，服务未正确启动；请先修复部署配置。” |
| `ASSEMBLYAI_API_KEY` 缺失 | “未配置转录 provider，当前无法开始真实转录。” |
| 当前翻译路由没有可用 provider key | “未配置可用翻译 provider，当前无法执行真实翻译。” |
| MiniMax TTS 运行配置缺失（`base_url` / `model_name` / `api_protocol`） | “MiniMax TTS 运行配置未完成，当前无法继续真实配音。” |
| 任务未提供 `voice_id` | “当前未指定 `voice_id`，将按后端现有逻辑尝试音色库匹配或自动克隆。” |
| Voice 解析仍失败（既无显式 `voice_id`，也无可用 voice registry / clone 结果） | “当前未能解析可用音色；请补充任务级 `voice_id` 或完成音色准备。” |
| Voice clone 配置缺失 | “当前链路需要 voice clone，但相关配置缺失。” |
| YouTube cookies 缺失且视频要求登录/反爬验证 | “当前视频需要有效 YouTube cookies，请补充 `cookie_file` 或浏览器 cookies 配置。” |
| `ffmpeg` 缺失 | “本机缺少 `ffmpeg`，当前无法完成媒体抽音与处理。” |
| Gateway 或 `/job-api/*` 反代缺失 | “API 入口链路未正确部署；这是部署问题，不是页面问题。” |
| 公网 Basic Auth 环境变量缺失 | “公网入口未完成认证配置，请先完成部署侧环境变量设置。” |

## 3. 这些提示应该落在哪个页面或检查环节

| 问题类型 | 最合适的提示位置 |
| --- | --- |
| 本地配置文件解析错误 | 运行前检查 |
| `remote_workbench.local.json` 无效 | 运行前检查 |
| 公网 Basic Auth 缺失 | 运行前检查 |
| Caddy / Gateway 反代链路缺失 | 运行前检查 |
| `ffmpeg` 缺失 | 运行前检查 |
| `ASSEMBLYAI_API_KEY` 缺失 | 新建翻译页前置提示；若仍进入运行，则当前任务页 / 项目详情页标记为“运行配置问题” |
| 翻译 provider key 缺失 | 新建翻译页前置提示；若仍进入运行，则当前任务页 / 项目详情页标记为“运行配置问题” |
| MiniMax `base_url` / `model_name` / `api_protocol` 缺失 | 运行前检查；若在运行中才暴露，则当前任务页 / 项目详情页标记为“运行配置问题” |
| 任务未提供 `voice_id` | 新建翻译页只做可选输入或说明，不默认拦截创建 |
| Voice 解析失败（含 registry / clone 无法完成） | 当前任务页 / 项目详情页明确标记为“运行配置问题”或“需要 review 处理” |
| Voice clone 配置缺失 | 新建翻译页不前置暴露系统级参数；若后端自动链路需要且失败，则当前任务页 / 项目详情页标记为“运行配置问题” |
| YouTube cookies 缺失 | 新建翻译页给“条件性提醒”；真正触发下载失败时在当前任务页 / 项目详情页明确提示 |
| Gateway / API 入口反代错误 | 运行前检查；页面只需提示”API 入口不可达是部署问题” |

## 4. 哪些属于页面问题，哪些属于现网配置问题

### 页面问题

- 新前端没有把配置类错误标成“运行配置问题”
- 当前任务页或项目详情页不能正确显示后端已返回的错误摘要
- review 入口文案、状态回看与真实后端能力不一致
- 新建翻译页若把 `base_url` / `model_name` 这类系统级参数错误地下放到任务表单，属于职责越界
- 新建翻译页若把“未设置 `voice_id`”误判成硬性阻断，而不是交给后端自动决策，也属于页面问题
- 页面把部署问题误导成“任务不存在”或“页面空态”

### 现网配置问题

- 缺少 provider / API key
- 缺少 MiniMax `base_url` / `model_name` / `api_protocol`
- 缺少受限视频所需 cookies
- 缺少 `ffmpeg`
- `autodub.local.json` 或 `remote_workbench.local.json` 非法
- `/job-api/*`、`/api/*` 反代缺失（所有 API 流量经由 Gateway 8880）
- 公网 Basic Auth 环境变量缺失
