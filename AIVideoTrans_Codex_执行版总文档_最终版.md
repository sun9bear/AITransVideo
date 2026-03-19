# AIVideoTrans 重构执行说明（给 Codex）

## 1. 执行目标

本次重构的目标不是继续堆新功能，而是把项目从“旧 pipeline 与新 workflow 并存”的状态，推进到“**单一公共主干 + 统一中间模型 + 双输出后端**”的稳定雏形。当前项目的目标产品形态已经明确为双输出：一类面向直接交付的 `Publish Output`，一类面向二次编辑的 `Editor Output`；`draft` 不再作为唯一终点，而是 `Editor Output` 的一个子能力。

本轮必须达成以下最小闭环：

1. 建立统一中间模型：
   - `LocalizedProject`
   - `ArtifactIndex`

2. 将音频资产纳入公共主干资产体系：
   - `source.original_audio`
   - `working.speech_for_asr`
   - `working.ambient_audio`
   - `audio.dubbed_full`

3. 让 workflow 返回统一构建结果：
   - `WorkflowBuildResult`

4. 建立统一输出分发器：
   - `OutputDispatcher`
   - 支持 `publish / editor / both`

5. 跑通最小双输出：
   - `Editor Package`
   - `Publish MP4`

以上属于本轮重构的核心交付物。

### 硬目标

以下事项属于本轮必须完成的硬目标：

- `LocalizedProject` / `ArtifactIndex` / `WorkflowBuildResult` 建立完成
- 音频准备能力进入公共主干资产体系
- `Editor Output` 最小闭环可用
- `Publish Output` 最小闭环可用
- `OutputDispatcher` 接入主流程

### 软目标

以下事项可以推进，但不能反过来阻塞硬目标：

- draft 输出结构进一步完善
- manifest 内容进一步丰富
- `dubbed_video_subtitled.mp4` 支持
- 原音轻混的初版能力
- 更完整的目录语义收口

---

## 2. 执行边界

当前仓库存在两条能力线：

- 旧成品素材包链路，以 `src/pipeline/process.py` 为核心，当前仍是最实用的可用闭环；
- 新模块化 workflow 架构，以 `src/modules/workflow/*` 及各模块目录为核心，代表未来方向。

本轮处理原则如下：

- 允许暂时保留旧链路兼容；
- 新能力可以暂时接到旧链路上，但最终必须回归公共主干；
- 不要继续制造第三条支线；
- 不要把 `draft` 继续当成唯一主产物；
- 不要大面积改 UI；
- 不要优先做真实剪映导入；
- 不要一次性删除全部 legacy；
- 不要为了命名或文件移动做大面积无收益重命名；
- 先让结构成立，再逐步收口 legacy。

---

## 3. 目标架构

目标架构统一为：

**单一公共主干 + 统一中间模型 + 双输出后端**

目标数据流如下：

`Input -> Intake -> Audio Preparation -> Media Understanding -> Translation -> Semantic Chunking -> TTS Synthesis -> Alignment -> Canonical Project Builder -> Output Dispatcher -> Publish Backend / Editor Backend`

其中新增的关键公共阶段是 `Audio Preparation`。这一步负责提取原始音频、生成人声 ASR 轨、生成环境音轨并登记为标准资产。当前仓库里这部分能力已经在旧 `process.py` 链路中局部实现，但尚未进入全局主干，因此本轮必须把它上收。

---

## 4. 模块职责约束

### 4.1 `modules/workflow/*`

只负责：
- 阶段编排
- state/cache 管理
- 构建 canonical project

不直接负责：
- draft 写出
- 成片渲染
- 编辑素材包导出

### 4.2 `modules/draft/*`

保留以下职责：
- draft schema
- material mapping
- caption retiming
- jianying-like export
- validation

不再承担：
- 项目最终输出总入口
- workflow 的唯一终点语义

### 4.3 `modules/output/*`

统一承担最终产物写出，拆分为：
- `modules/output/editor/*`
- `modules/output/publish/*`
- `modules/output/output_dispatcher.py`
- `modules/output/manifest_writer.py`
- `modules/output/file_layout.py`

---

## 5. 目标目录与文件改造

请以以下结构为目标进行增量重构，不要求一次性全部完成，但命名和职责尽量按此执行：

```text
src/
  core/
    enums.py
    models.py
    project_model.py
    artifact_index.py

  modules/
    ingestion/
    media_understanding/
    translation/
    chunking/
    alignment/
    tts/
    workflow/
      project_workflow.py
      project_builder.py
      workflow_result.py
      ...

    draft/
      caption_retiming.py
      draft_writer.py
      material_mapper.py
      schema_validator.py
      export_schema.py
      jianying_adapter.py
      jianying_export_validator.py

    output/
      output_dispatcher.py
      manifest_writer.py
      file_layout.py
      output_models.py

      editor/
        editor_package_backend.py
        editor_package_writer.py
        draft_backend.py

      publish/
        publish_backend.py
        video_renderer.py
        subtitle_burner.py
        audio_mixer.py

  pipeline/
    legacy_process.py
```

其中：

- `src/pipeline/process.py` 当前**逻辑上视为 legacy**，本轮允许继续复用；如重构推进顺利，后续再视情况迁移为 `legacy_process.py`；
- `src/modules/output/project_output.py` 不再继续作为“大一统输出器”，应逐步拆分到 editor / publish 两侧；
- 本轮重点是职责收口，不要求第一时间完成全部文件物理迁移。

---

## 6. 必须实现的核心对象与接口

### 6.1 Core 层

#### `LocalizedProject`

文件：
- `src/core/project_model.py`

用于承载 workflow 主干处理结束后的标准项目模型。

**第一版必须包含的最小字段：**
- `project_id`
- `source_info`
- `artifacts`
- `stage_snapshot`

**第一版尽量包含，但不是阻塞硬要求的字段：**
- `semantic_blocks`
- `aligned_blocks`
- `captions`

说明：
- `LocalizedProject` 是 canonical domain model；
- output backend 不应绕过它，直接从零散 stage payload 或零散文件重新拼主语义；
- 本轮不要再单独把 `subtitle_lines` 作为第一版必备字段，避免与 `captions` 形成重复语义。

#### `ArtifactIndex`

文件：
- `src/core/artifact_index.py`

用于统一管理项目产物路径与索引。至少提供：
- `register(key, path)`
- `get(key)`
- `require(key)`
- `to_dict()`

建议标准资产键至少包括：
- `source.original_video`
- `source.original_audio`
- `working.speech_for_asr`
- `working.ambient_audio`
- `working.transcript`
- `working.literal_translation`
- `working.tts_rewrite`
- `audio.segment_dir`
- `audio.dubbed_full`
- `editor.subtitles_srt`
- `publish.video_mp4`

说明：
- `ArtifactIndex` 是资产索引，不是 `LocalizedProject` 的替代品；
- 它负责“文件和工件在哪里”，不负责承载主业务语义。

#### `WorkflowBuildResult`

文件：
- `src/modules/workflow/workflow_result.py`

至少包含：
- `project_id`
- `localized_project`
- `artifact_index`
- `stage_snapshot`

---

### 6.2 Workflow 层

#### `ProjectBuilder`

文件：
- `src/modules/workflow/project_builder.py`

职责：
- 从 stage outputs + artifact index 构建 `LocalizedProject`

#### `ProjectWorkflow.run_build()`

文件：
- `src/modules/workflow/project_workflow.py`

职责：
- 跑主干 stages
- 收集 stage outputs
- 更新 `ArtifactIndex`
- 调用 `ProjectBuilder.build(...)`
- 返回 `WorkflowBuildResult`

要求：
- 保留旧 `run()` 兼容入口；
- `run()` 可内部转发到 `run_build()`；
- 本轮不要在这一步同时改大规模输出逻辑。

---

### 6.3 Output 层

#### `OutputTarget`

文件：
- `src/core/enums.py`

至少包括：
- `PUBLISH`
- `EDITOR`
- `BOTH`

#### `OutputRequest`

文件：
- `src/modules/output/output_models.py`

至少支持：
- `targets`
- `burn_subtitles`
- `mix_original_audio`
- `include_draft_export`

#### `OutputBundleResult`

文件：
- `src/modules/output/output_models.py`

至少支持：
- `publish_result`
- `editor_result`
- `manifest_path`

#### `OutputDispatcher`

文件：
- `src/modules/output/output_dispatcher.py`

职责：
- 根据输出模式分发到不同 backend

接口建议：
- `dispatch(localized_project, artifact_index, request) -> OutputBundleResult`

---

### 6.4 Editor Output

#### `EditorPackageBackend`

文件：
- `src/modules/output/editor/editor_package_backend.py`

职责：
- 编辑向输出总入口

#### `EditorPackageWriter`

文件：
- `src/modules/output/editor/editor_package_writer.py`

职责：
- 生成可供二次编辑的素材包

最小输出内容：
- `subtitles.srt`
- `timeline.json`
- `alignment_report.md`
- `dubbed_audio_complete.wav`
- 分段音频目录
- `editor_manifest.json`

#### `DraftBackend`

文件：
- `src/modules/output/editor/draft_backend.py`

职责：
- 调用 `draft/*` 生成 internal draft / jianying-like export；
- `draft` 必须作为 Editor Output 子能力接入，不再作为 workflow 唯一终点。

---

### 6.5 Publish Output

#### `PublishBackend`

文件：
- `src/modules/output/publish/publish_backend.py`

职责：
- 成品视频输出总入口

**本轮成功标准：**
- 仅要求可稳定生成最小 `dubbed_video.mp4`；
- 不要求本轮同时完成字幕烧录、环境音混音、发布预设抽象。

#### `VideoRenderer`

文件：
- `src/modules/output/publish/video_renderer.py`

职责：
- 原视频 + 中文配音 -> 最终视频
- 原视频 + 中文配音 + 字幕 -> 字幕版视频

最小先做：
- `dubbed_video.mp4`

如进度允许再做：
- `dubbed_video_subtitled.mp4`

#### `AudioMixer`

文件：
- `src/modules/output/publish/audio_mixer.py`

职责：
- 中文配音音量控制
- 原音轻混
- 最终 mixdown

本轮可以只建立接口与最小能力，不要求复杂混音完成。

#### `SubtitleBurner`

文件：
- `src/modules/output/publish/subtitle_burner.py`

职责：
- 字幕烧录辅助

---

## 7. Captions / Subtitle 工件语义约束

为避免本轮实现中出现重复语义，请统一采用以下约束：

- `captions`：`LocalizedProject` 内的 canonical caption representation；
- `working.retimed_captions`：`captions` 的持久化工件或中间文件；
- 本轮不再单独引入 `subtitle_lines` 作为第一版必备字段；
- `subtitles.srt`：面向 Editor / Publish 输出的最终字幕工件之一，不等于 canonical model 本身。

---

## 8. Audio Preparation 边界与最低契约

本轮必须把音频准备能力正式上收为公共主干能力。建议新增：

- `AudioPreparationStage`
或
- `SourceAudioPreparationService`

其最低契约如下：

### 输入类型最低支持
- `youtube_url`
- `local_video`
- `local_audio`

### 目标产物
- `source.original_audio`
- `working.speech_for_asr`
- `working.ambient_audio`（如可生成）

### 行为约束
- 若环境音分离成功，应登记 `working.ambient_audio`；
- 若环境音分离失败，不得直接让整个 workflow 失败；
- 若 separator 失败，应支持回退到原始音频或可用音频继续主流程；
- 回退结果必须在 state / artifact / manifest 中可见；
- 命名上统一使用 `ambient_audio` 语义，不再长期混用 `ambient.wav` / `ambient_audio.wav` 作为概念名。

### 测试最低要求
至少补两类测试：
- 正常生成
- 缺失或失败时回退

---

## 9. 执行顺序

请严格按以下顺序推进，不要跳步大改。

### 阶段 0：冻结现状

- 建立重构分支
- 给当前版本打 tag，例如 `pre-dual-output-refactor`
- 标记 legacy 文件：
  - `src/pipeline/process.py`
  - `src/modules/output/project_output.py`
  - `main.py` 中旧入口
- 新建或更新 `CURRENT_PROJECT_STATUS.md`，明确：
  - 当前主目标：双输出
  - 当前主干：workflow 为未来方向
  - 当前可用闭环：legacy `process.py`
  - draft 定位：editor 子能力，不再视为唯一终点

### 阶段 1：建立统一中间模型

- 新建 `project_model.py`
- 新建 `artifact_index.py`
- 新建 `workflow_result.py`
- 新建 `project_builder.py`
- 给 `ProjectWorkflow` 增加 `run_build()`
- 保留旧 `run()` 兼容

### 阶段 2：音频资产标准化并上收

- 把 `speech_for_asr` / `ambient_audio` 纳入统一资产体系
- 统一命名语义，不再长期混用 `ambient.wav` / `ambient_audio.wav`
- 新增 `AudioPreparationStage` 或 `SourceAudioPreparationService`
- 允许内部先调用 legacy 已有实现
- 要支持失败回退
- 要写入 state / manifest / artifact index
- 补至少两类测试：
  - 正常生成
  - 缺失或失败时回退

### 阶段 3：先形成 Editor Output 最小闭环

- 从 `project_output.py` 迁出编辑向输出能力到 `EditorPackageWriter`
- 新增 `DraftBackend`
- 先让 `Editor Package` 成为正式输出类型

### 阶段 4：再形成 Publish Output 最小闭环

- 新增 `PublishBackend`
- 新增 `VideoRenderer`
- 先实现最小成品视频输出：
  - 原视频 + `dubbed_audio_complete.wav` -> `dubbed_video.mp4`

### 阶段 5：接入统一输出分发

- 新增 `OutputDispatcher`
- workflow build 完成后统一进入 dispatcher
- CLI 增加：
  - `--output publish`
  - `--output editor`
  - `--output both`
- 可选再加：
  - `--burn-subtitles`
  - `--mix-original-audio`

### 阶段 6：收口与清理

- 补关键测试
- 更新 `README.md`
- 更新 `CURRENT_PROJECT_STATUS.md`
- 输出 `REFACTOR_PHASE1_SUMMARY.md`
- 视进度将 `process.py` 迁移或明确标注为 `legacy_process.py`
- 入口层只保留：
  - 参数解析
  - mode 分发
  - workflow 启动
  - output dispatcher 调用

---

## 10. 输出目录目标

输出目录逐步向以下语义统一，不要求本轮一步到位，但新代码尽量朝这个结构靠拢：

```text
project_root/
  project_state.json
  project_cache.json
  manifest.json

  source/
    original.mp4
    original_audio.wav
    source_transcript.json

  working/
    translated_lines.json
    semantic_blocks.json
    aligned_blocks.json
    retimed_captions.json
    speech_for_asr.wav
    ambient_audio.wav

  audio/
    segments/
    dubbed_audio_complete.wav

  editor_package/
    subtitles.srt
    alignment_report.md
    timeline.json
    editor_manifest.json

  draft/
    draft_content.json
    draft_meta_info.json
    jianying_like_export.json

  publish/
    dubbed_video.mp4
    dubbed_video_subtitled.mp4
    publish_manifest.json
```

这一步的重点是统一语义，而不是一次性追求所有命名完美。

---

## 11. Manifest 最低要求

本轮 `manifest` 不是最高优先级，但也不应完全后置。最低要求如下：

- 最终至少生成一个最小 `manifest.json`；
- 至少记录：
  - source 信息
  - 关键音频资产路径
  - 主要输出路径
  - 关键 fallback 情况
  - 主要输出模式（publish / editor / both）

如果时间不足，可以先实现最小 `ManifestWriter`，后续再扩展字段。

---

## 12. 本轮明确不做的事

本轮不要优先做以下事项：

- 不优先做真实剪映导入
- 不做大规模 UI 改版
- 不一次性删除全部 legacy
- 不优先做复杂混音和大量发布预设
- 不优先做高级字幕样式
- 不自行扩大需求范围
- 不因为“想更优雅”而重写整条旧链路

这些事项都不应阻塞本轮主线目标。

---

## 13. 提交与验收要求

请按阶段推进，每完成一个阶段就形成一次**可运行、可回滚、可测试**的提交。每次提交前必须满足：

- 至少一条主流程最小 case 能跑通
- 关键测试通过或失败原因明确
- 没有新增第三条隐藏支线
- `publish` / `editor` 边界比改动前更清晰
- 文档与代码口径一致

本轮完成后的验收口径如下：

- 项目已从“旧 pipeline 与新 workflow 并存”推进到“统一主干雏形”
- 音频分离能力已进入公共资产体系
- 输出层已明确拆分为 `Publish` 与 `Editor`
- `draft` 不再是唯一终点，而是 `Editor Output` 的子能力
- 系统可以一次处理后，按需输出：
  - `Editor Package`
  - `Publish MP4`

---

## 14. 执行方式要求

请按以下方式执行：

- 以“最小可运行重构”为原则
- 优先保留兼容，不要一次性重写整个项目
- 新增文件和类名尽量按本文指定命名
- 先让结构成立，再做细节优化
- 遇到不确定项，优先选择更小、更保守、可测试的改法
- 不要擅自扩大范围，不要自行转向 UI 或真实剪映导入方向

---

## 15. 给 Codex 的附加指令

请严格按本文执行，不要自行扩大范围，不要重写整个项目。请按阶段推进，每完成一个阶段先保证代码可运行、测试可通过，再进入下一阶段。
