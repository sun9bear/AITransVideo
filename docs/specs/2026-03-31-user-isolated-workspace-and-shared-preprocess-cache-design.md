# 用户隔离工作区与共享预处理缓存设计

> 状态: 一期已落地（Task 1-7 完成），二期共享缓存 Deferred
> 日期: 2026-03-31
> 适用范围: Gateway / Job API / Web UI / Process Runner / Pipeline / Frontend Next

## 1. 背景

当前分支已经引入了多用户任务、上传本地视频、套餐并发限制等商业化相关能力，但底层目录与任务复用模型仍然沿用单用户脚本时代的假设：

- `JobService.submit_job()` 仍保留全局单活跃任务闸门，导致 Plus / Pro 套餐无法真正并发。
- pipeline 仍通过 YouTube URL 或标题 slug 推导项目目录，缺少用户边界。
- 本地上传入口已经存在，但后续 runner / CLI / pipeline 仍默认把 `source_ref` 当作 `youtube_url`。
- 任务收尾阶段与日志解析仍依赖旧的 Windows 路径假设，无法稳定回填 Linux / container 环境下的项目目录。

如果继续在全局 `projects/` 目录中按 URL 直接复用项目工作区，会出现三个不可接受的问题：

1. 不同用户可能误复用同一个工作区，造成人工审核数据串线。
2. 进入 review 阶段后的中间产物带有用户编辑语义，不再适合跨任务共享。
3. 本地上传文件没有稳定的全局来源键，无法用“目录复用”模型安全复用。

因此需要把“工作区隔离”和“预处理缓存复用”分离成两层机制。

## 2. 目标、非目标与分期策略

### 2.1 目标

1. 为每个任务建立稳定、可定位、与用户绑定的独立工作区。
2. 打通 YouTube 与本地上传两种 source type 的端到端执行链路。
3. 去掉对全局 URL 项目目录复用的依赖，改为显式路径与任务快照驱动。
4. 允许不同用户在后续阶段复用审核前、只读、可验证的预处理结果，避免重复下载与重复转录。
5. 保持设计轻量、可测试，并符合 Sprint 1 “无真实外部 API、main.py 与 pytest 必须可运行”的约束。

### 2.2 非目标

- 本阶段不实现跨机器分布式缓存。
- 本阶段不实现复杂计费或按缓存命中返现逻辑。
- 本阶段不复用 review 后的翻译、TTS、对齐、manifest 或下载包。
- 本阶段不重写整条 pipeline，只在关键边界处补齐 source-aware 与 path-aware 能力。

### 2.3 分期策略

结合当前代码状态与产品阶段，本文采用“两期设计、分步落地”的策略：

1. 一期先解决安全性与可用性问题：
   `user_id + job_id` 隔离工作区、上传目录隔离、并发闸门移除、`source_type` 打通、旧复用逻辑清理、Linux/旧目录兼容。
2. 二期再引入共享预处理缓存：
   仅在有真实多用户重复来源压力后，补充只读 `cache/` 层与指纹/锁机制。

这意味着共享 cache 仍是目标态架构的一部分，但不是当前版本的阻塞项。

## 3. 核心设计决策

### 3.1 工作区必须按 `user_id + job_id` 隔离

每个任务唯一工作区固定为：

`projects/<user_id>/<job_id>/`

其中：

- `user_id` 直接使用 gateway 当前稳定的用户主键，不再额外引入 `owner_id` 概念。
- `job_id` 为任务主键，保证同一用户的多任务也不会相互覆盖。
- slug、视频标题、URL 只作为展示元数据，不再参与工作区唯一性判断。

### 3.2 上传暂存区必须按用户隔离

上传文件统一写入：

`uploads/<user_id>/<upload_id>_<safe_name>`

其中 `upload_id` 使用 UUID 或等价的全局唯一键，避免“同一秒同名文件覆盖”。

如果现有 Web UI 上传接口暂时拿不到认证态 `user_id`，则必须先补齐受信任的用户上下文注入，再切换到该目录规则；不能继续使用全局共享 `uploads/`。

### 3.3 不再按 URL 直接复用项目目录

`_find_existing_project_by_url()` 这类逻辑不再允许直接返回别人或旧任务的工作区。对于新任务：

- 一期：同源任务也必须创建独立 workspace。
- 二期：如需复用，只允许命中共享只读 cache，而不是直接复用整个项目目录。

### 3.4 共享复用只能发生在审核前只读缓存层

二期引入全局共享缓存根目录：

`cache/`

缓存只允许存放以下“来源导向、审核前、不可变”的产物：

- 下载后的原始视频或原始媒体引用
- 从来源中提取/分离出的稳定音频文件
- 原始 ASR transcript
- 尚未进入人工审核的结构化 transcript

以下产物明确禁止进入共享 cache：

- speaker 命名与 reviewer 标注
- 人工修订 transcript
- 翻译稿、术语修订、字幕审核状态
- voice mapping、TTS 结果、对齐结果
- manifest、剪映草稿、下载包

## 4. 存储结构

### 4.1 一期落地结构

```text
data/
  projects/
    <user_id>/
      <job_id>/
        input/
        work/
        review/
        output/
        manifests/
  uploads/
    <user_id>/
      <upload_id>_<safe_name>
```

### 4.2 二期目标结构

```text
data/
  projects/
    <user_id>/
      <job_id>/
        input/
        work/
        review/
        output/
        manifests/
  uploads/
    <user_id>/
      <upload_id>_<safe_name>
  cache/
    source/
      youtube/<source_key>/
      local/<file_sha256>/
    derived/
      audio/<source_blob_key>/<audio_fingerprint>/
      transcript/<source_blob_key>/<transcript_fingerprint>/
```

说明：

- `projects/` 是可变工作区，只属于单个任务。
- `uploads/` 是短生命周期暂存区，可在任务起跑后清理。
- `cache/` 是二期再引入的只读共享层。

## 5. 数据模型变更

### 5.1 Job snapshot 增加 `user_id` 与 `workspace_dir`

Job 创建时，gateway 需要把以下字段写入 job snapshot，并由 Job API 持久化：

- `user_id`
- `source_type`
- `source_ref`
- `source_content_hash`（本地上传场景可选）
- `workspace_dir`

同时保留现有 `project_dir` 字段作为兼容层：

- 对新任务，`workspace_dir` 是规范字段，`project_dir` 可在需要时同步为同一路径。
- 对旧任务，`workspace_dir` 缺失时仍允许继续读取历史 `project_dir`。

### 5.2 `source_type` 成为执行链路的一等输入

当前 `youtube_url` 语义过重，需要改成更通用的来源建模：

- `source_type = youtube_url`
- `source_type = local_video`
- `source_type = local_audio`

CLI、runner、pipeline 配置层都必须显式接收 `source_type`，不能再把所有 `source_ref` 塞进 `youtube_url` 位置参数。

## 6. 二期缓存键与指纹设计

### 6.1 来源键

YouTube 来源键：

- 由 canonical URL 或 video id 规范化得到 `source_key`

本地上传来源键：

- 由文件内容 `sha256` 生成 `source_key`

### 6.2 派生产物指纹

音频类缓存指纹至少包含：

- 来源键
- 音频提取/分离策略版本
- 关键参数版本

转录类缓存指纹至少包含：

- 来源键
- `transcription_method`
- provider / model
- diarization 配置版本
- prompt 或结构化后处理版本

只有当输入来源与算法指纹都完全一致时，才允许复用缓存。

## 7. 端到端流程

### 7.1 创建任务

1. frontend 从 entitlements 获取当前用户与并发上限。
2. gateway 基于 `user_id` 和套餐规则决定是否允许新任务创建。
3. gateway 生成 `workspace_dir`，写入 job snapshot。
4. Job API 持久化 snapshot，不再以“系统中是否已有任何 active job”作为拒绝条件。

### 7.2 一期 YouTube 任务

1. runner 按 `source_type=youtube_url` 调起 pipeline。
2. pipeline 在 `projects/<user_id>/<job_id>/` 下初始化独立工作区。
3. 继续沿用当前下载与处理链路，但不再复用旧项目目录。
4. review 后产物继续只写当前 workspace。

### 7.3 一期本地上传任务

1. Web UI 将文件写入 `uploads/<user_id>/<upload_id>_<safe_name>`。
2. 服务端可选计算 `sha256` 并写入 job snapshot 的 `source_content_hash`，为二期缓存预留。
3. runner 按 `source_type=local_video` 或 `local_audio` 调起 pipeline。
4. pipeline 直接从本地来源进入 ingest，不再把本地路径误当成 `youtube_url`。

### 7.4 二期共享缓存任务

二期在不改变工作区隔离规则的前提下，引入：

1. source cache lookup / publish
2. derived audio cache lookup / publish
3. transcript cache lookup / publish
4. cache 命中后向当前 workspace hydrate 只读输入

## 8. 二期缓存发布与并发控制

为避免两个任务同时构建同一个 cache key，cache 写入应遵循：

1. 先写入临时目录或临时文件。
2. 写入完成后通过原子 rename 发布。
3. 使用轻量锁文件或目录锁避免双写。
4. 任何消费者只读取“已发布完成”的缓存路径。

这套机制必须保持本地文件系统可测试，不引入额外外部依赖。

## 9. 结果定位与迁移兼容

### 9.1 优先使用 snapshot 中的 `workspace_dir`

`process_runner` 收尾阶段应优先以任务 snapshot 中的 `workspace_dir` / `project_dir` 为准，而不是再根据 URL 或日志猜目录。

### 9.2 日志解析必须兼容 Windows 与 POSIX 路径

S6 输出目录日志仍然可以作为兜底线索，但解析器必须同时支持：

- `D:\...`
- `/opt/...`

### 9.3 旧任务不迁移，只保留只读兼容

迁移策略明确如下：

1. 旧 job 若缺少 `workspace_dir`，则继续读取已有 `project_dir`。
2. 旧 job 若 `project_dir` 为 `null` 或旧格式，才 fallback 到原有路径解析逻辑。
3. 新建 job 一律写入 `projects/<user_id>/<job_id>/`。
4. Web UI / read surface 读取结果时，先看 `workspace_dir`，没有再走旧逻辑。
5. 不做历史目录数据迁移，旧 `projects/<slug>/` 与 `data/projects/...` 原地保留。

## 10. 安全与隔离要求

1. 不同用户即使提交同一个 YouTube URL，也必须生成不同的 job 与 workspace。
2. 不同用户即使上传内容完全相同的视频，也必须生成不同的 job 与 workspace。
3. review 后的任何产物都不能跨任务共享。
4. 任何用户可编辑文件都只能位于自己的 `projects/<user_id>/<job_id>/` 下。

## 11. 测试策略

### 11.1 一期必测

- 不同用户相同 YouTube URL 可以分别创建任务，且 workspace 不相同。
- `_resolve_job_project_dir()` 能基于新 workspace 结构定位目录，并兼容 `data/projects`。
- `process_runner` 能解析 POSIX 输出目录日志。
- 本地视频任务不会再走 `youtube_url` 解析链路。
- Plus / Pro 用户可以在套餐上限内并发创建任务。
- source-type 改造后，`job_id -> snapshot -> TTSGenerator` 的 per-job TTS 路由仍然成立。

### 11.2 二期补测

- 不同用户相同本地视频文件可以分别创建任务，且 source cache 可共享。
- cache 命中可跳过重复下载 / 分离 / 转录。
- review 后产物不会回写共享 cache。

## 12. 实施结论

本设计采用“用户隔离工作区 + 二期共享只读预处理缓存”的双层结构：

- 一期先用 `projects/<user_id>/<job_id>/` 解决用户隔离、并发闸门、上传链路与旧路径兼容问题。
- 二期再用 `cache/` 解决重复下载、重复分离、重复转录的成本问题。
- 用显式 `source_type` 统一 `youtube_url` 与本地上传两条链路。

这比当前分支更贴近已有代码现实，也更适合作为本轮实施的正式目标。
