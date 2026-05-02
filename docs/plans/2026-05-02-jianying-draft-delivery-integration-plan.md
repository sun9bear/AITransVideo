# 可打开剪映草稿交付接入方案

> Status: Approved
> Last updated: 2026-05-02
> Scope: 生成一个可被剪映专业版桌面端识别并打开的新草稿，作为本项目最终交付物之一；不把剪映自动渲染 MP4 纳入主链目标。

## 1. 结论

技术上可行，但必须把目标限定为“生成可打开的新草稿文件夹/草稿包”，而不是官方支持的工程导入 API。剪映/CapCut 草稿本质上是项目目录加若干 JSON 和资源文件，社区已有多个项目通过写 `draft_content.json`、`draft_meta_info.json`、素材目录来生成可打开草稿。

本项目当前已经有 draft-first 架构，但现有 `DraftWriter` 输出的是内部 scaffold 和 `jianying_like_export.json`，不是可直接打开的剪映私有格式。接入方向应当是新增一个真实剪映草稿后端，挂在最终 output delivery 层，复用现有确定性时间轴和素材，不改变 TTS / alignment / caption retiming 的核心边界。

重要前置条件：进入主链的剪映草稿包不得复用当前 SRT 二次切片逻辑。主链 Phase 1 前必须完成 `docs/plans/2026-05-02-subtitle-cue-generation-v2-plan.md` 的 gate，生成 canonical `SubtitleCue`，确保字幕文本来自实际 TTS 文本、语义切分稳定、时间点确定。剪映草稿后端只能消费这套 canonical cue，不允许自己重新切字幕或重新分配时间；独立 Phase 0 spike 可以先用现有 SRT 验证格式可行性。

## 1.1 前置 Gate：字幕生成流程 v2

在启动本方案 Phase 1 前，必须满足：

- 已生成 `editor.subtitle_cues` / `subtitle_cues.json`。
- 已生成 `editor.subtitle_quality_report` / `subtitle_quality_report.json`。
- `subtitle_quality_report.validation_status` 为 `passed`，或为 `needs_review` 但没有 text mismatch / timing overlap 等硬错误。
- 下载 SRT、internal draft caption、未来剪映草稿字幕都来自同一组 canonical `SubtitleCue`。
- 任一 block 的字幕 cue 文本归一化拼接后，必须等于该 block 实际送入 TTS 的最终 `merged_cn_text`。

注意：这里约束的是进入主链的 Phase 1 后端 PoC。Phase 0 技术 spike 可以与字幕 v2 Phase 1a 并行，用现有 SRT 验证 `pyJianYingDraft` 和剪映桌面端版本兼容性；spike 结果不得直接接入主链，也不得绕过后续 canonical cue gate。

未满足 gate 时：

- pipeline 仍可交付现有配音视频、配音音频、SRT 和素材包。
- 不生成 `editor.jianying_draft_zip`。
- manifest / 兼容报告记录 skipped reason: `subtitle_cues_not_ready` 或具体校验错误。

## 2. 外部依据

优先参考：

- `GuanYixuan/pyJianYingDraft`: 活跃度最高，支持 Python 生成剪映草稿，覆盖音视频、文本、字幕、轨道、转场、特效等。PyPI 最新版 `0.2.6` 于 2026-03-16 发布。
- `GuanYixuan/pyCapCut`: 同作者 CapCut 版本，可作为国际版结构对照。
- `notinmood/JianyingDraft.PY` / `xiaoyiv/JianYingProDraft`: 更早期实现，说明基础原理是创建 `draft_content.json`、`draft_meta_info.json`，并建立素材库、内容素材、轨道片段之间的引用。
- `vogelcodes/capcut-srt-export`: 不是生成器，但证明字幕等内容可通过 CapCut 草稿 JSON 读取和修改。

关键限制：

- 剪映 6+ 对 `draft_content.json` 模板读取有加密限制，社区库的“模板模式”只可靠覆盖 5.9 及以下；但从零生成基础音视频/字幕草稿仍有社区项目声称支持 5+。
- 批量自动导出依赖 UI 自动化，剪映 7+ 控件隐藏后风险较高。本方案不依赖自动导出。
- 官方 CapCut 帮助文档明确不支持把一个项目直接导入另一个项目并保留可编辑层，因此产品口径应是“下载/打开一个新草稿”，不是“导入到现有项目”。

## 3. 当前项目落点

现有链路：

```mermaid
graph TD
    Workflow["ProjectWorkflow.run_build()"] --> DraftStage["DraftStageRunner"]
    DraftStage --> InternalDraft["DraftWriter: internal scaffold"]
    Workflow --> LocalizedProject["LocalizedProject"]
    LocalizedProject --> Dispatcher["OutputDispatcher"]
    Dispatcher --> Editor["EditorPackageBackend"]
    Editor --> OutputFiles["output/: dubbed_audio, subtitles, segments, reports"]
    Dispatcher --> Publish["PublishBackend optional"]
    Publish --> DubbedVideo["publish.dubbed_video"]
    Dispatcher --> Manifest["manifest.json + artifact_index"]
    Manifest --> JobAPI["Job API artifacts/download"]
    JobAPI --> Gateway["Gateway proxy/R2/local fallback"]
    Gateway --> Frontend["ResultMediaCard downloads/materials pack"]
```

现有真实可交付文件主要在 `OutputDispatcher` 之后注册到 manifest：

- `editor.dubbed_audio_complete`
- `editor.ambient_audio`
- `editor.subtitles`
- `editor.subtitles_en`
- `editor.subtitles_bilingual`
- `editor.segments_dir`
- `publish.dubbed_video`

最合适的接入点是 `OutputDispatcher` 调用 editor backend 之后、写 manifest 之前。原因：

- 此时 `ProjectOutput` 已经汇总了项目 ID、总时长、对齐段落、分段音频、字幕文本。
- `EditorPackageWriter` 已经生成剪映友好的 WAV 和 SRT。
- `ArtifactIndex` 是最终下载、素材包和前端展示的统一入口。
- 不需要改动 translation / chunking / TTS / alignment 的核心阶段。

## 4. 目标草稿形态

PoC 目标只覆盖本项目当前最重要的可编辑交付：

- 原视频轨：使用 `source.original_video`。
- 配音轨：优先使用 `editor.dubbed_audio_complete`，作为整条配音音频。
- 字幕轨：优先使用 `editor.subtitles` 或 `editor.subtitles_bilingual` 导入为剪映文本字幕。
- 环境音轨：可选，使用 `editor.ambient_audio`，默认低音量或单独轨道。
- 项目尺寸：默认 1920x1080；后续从源视频探测宽高。
- 草稿名称：优先 `display_name` / `video_title` / `project_id`。

暂不覆盖：

- 花字、贴纸、复杂模板、转场、滤镜、关键帧。
- 剪映自动导出 MP4。
- 将生成草稿合并进用户已有草稿。
- full usage ledgering 或新的付费计量维度。

## 5. 设计方案

### 5.1 新增真实剪映草稿后端

新增模块建议：

```text
src/modules/output/jianying/
  __init__.py
  jianying_draft_backend.py
  jianying_draft_models.py
  jianying_draft_writer.py
  jianying_draft_validator.py
```

核心接口：

```python
@dataclass(slots=True)
class JianyingDraftRequest:
    project_id: str
    project_title: str
    source_video_path: str
    dubbed_audio_path: str
    subtitle_path: str
    output_dir: str
    ambient_audio_path: str | None = None
    width: int = 1920
    height: int = 1080


@dataclass(slots=True)
class JianyingDraftResult:
    draft_dir: str
    draft_zip_path: str
    draft_content_path: str
    draft_meta_info_path: str
    manifest_path: str | None
    compatibility_report_path: str
    validation_status: str
```

后端职责：

- 从 `ProjectOutputResult` 和 `ArtifactIndex` 解析输入素材。
- 调用 `pyJianYingDraft` 或内部 adapter 生成剪映项目目录。
- 将草稿目录打包成 zip，供浏览器下载。
- 写 `jianying_compatibility_report.json`，记录剪映版本、生成器版本、素材清单、验证结果。
- 不做网络调用，不调用剪映 UI，不自动渲染。

### 5.2 依赖策略

第一阶段建议使用 `pyJianYingDraft` 做 PoC，但要包在本项目自己的 adapter 后面：

```text
OutputDispatcher
  -> JianyingDraftBackend
      -> PyJianYingDraftAdapter
```

原因：

- 社区库已经覆盖草稿字段细节，能缩短验证周期。
- 本项目保留自己的 `JianyingDraftBackend` 边界，后续可以替换为自研 writer。
- 依赖应先做 optional dependency，不进入默认 `main.py` / `pytest` 必需路径。

建议开关：

```text
AVT_ENABLE_JIANYING_DRAFT=0/1
AVT_JIANYING_DRAFT_ENGINE=pyjianyingdraft/internal
AVT_JIANYING_DRAFT_WIDTH=1920
AVT_JIANYING_DRAFT_HEIGHT=1080
```

如果未安装 `pyJianYingDraft` 或开关未启用，pipeline 不失败，只跳过真实剪映草稿产物，并在 manifest 的兼容报告或日志里记录 skipped reason。

### 5.3 输出目录约定

建议写入：

```text
{project_dir}/jianying/
  draft/
    draft_content.json
    draft_meta_info.json
    materials/...
    ...
  exports/
    jianying_draft_{job_id_or_project_id}.zip
  jianying_compatibility_report.json
```

manifest artifact keys：

```text
editor.jianying_draft_dir
editor.jianying_draft_zip
editor.jianying_compatibility_report
```

这些 key 属于 editor 类产物，不属于 publish 产物。剪映草稿是主目标交付之一，但它仍是“编辑器工程产物”，不是已发布视频。

### 5.4 OutputDispatcher 接入

> ⚠️ **此节描述的"OutputDispatcher 自动生成"架构已在 phase 1 后端 PoC 完成后被废弃,改为 §11 描述的 on-demand 按需生成架构。**
>
> phase 1 后端 PoC(commits 6b9ce6b..36921e2)实施了本节方案,部署到 US 但 gate 关闭。由于产品决策(草稿 zip 1-2 GB,不是每个用户都需要,自动生成浪费资源),改为按需触发。后端模块 §5.1-§5.3 保留作为 backend 实现,本节的 OutputDispatcher 接入逻辑已在 commit `<K1 SHA>` 中回滚。
>
> **当前生效的架构见 §11**。本节保留作为历史记录。

建议扩展 `OutputRequest`，新增非破坏性选项：

```python
@dataclass(slots=True)
class OutputRequest:
    targets: list[OutputTarget] = field(default_factory=lambda: [OutputTarget.EDITOR])
    include_jianying_draft: bool = False
    service_mode: str | None = None
    ...
```

`src/pipeline/process.py::_dispatch_process_output_bundle()` 当前构造 `OutputRequest` 时需要显式打开开关，建议从环境变量注入：

```python
include_jianying = os.environ.get("AVT_ENABLE_JIANYING_DRAFT", "0") == "1"

OutputRequest(
    targets=[OutputTarget.PUBLISH],
    include_jianying_draft=include_jianying,
    service_mode=service_mode,
    ...
)
```

如果文件尚未导入 `os`，同步补充 import。`service_mode` 应从任务创建层、source info 或现有 job metadata 传入，不能只依赖前端 `serviceMode` 的 UI 可见性。

接入流程：

```python
editor_result = self.editor_backend.write(project_output)
self._register_editor_artifacts(artifact_index, editor_result)

if request.include_jianying_draft and request.service_mode == "studio":
    jianying_result = self.jianying_backend.write(
        self._build_jianying_request(
            localized_project,
            artifact_index,
            project_root,
            editor_result,
        )
    )
    self._register_jianying_artifacts(artifact_index, jianying_result)

manifest_path = self.manifest_writer.write(...)
```

注意事项：

- `source.original_video` 缺失时不要硬失败 editor 输出；真实剪映草稿标记 skipped。原因是本项目仍可能处理纯音频或字幕输入。
- `editor.dubbed_audio_complete` 是整轨，PoC 阶段比逐段音频更稳；逐段音频可后续用于更细粒度可编辑。
- Phase 0 spike 可临时使用现有 SRT 验证草稿能否打开；Phase 1 主链必须使用 canonical `SubtitleCue` 或由它序列化出的 SRT，不得再走旧 SRT 二次切片结果。
- 主链默认只在 `service_mode == "studio"` 时生成剪映草稿；Express 或缺失 mode 都应跳过并记录 `skipped_reason=service_mode_not_enabled`。本地 spike / dev 脚本需要显式传入 Studio 等价模式，不能依赖漏传 mode 放行。未来如产品决定 Express 也给草稿，再显式放开生成策略和下载白名单。

### 5.5 下载与前端接入

Job API：

- 若现有下载 surface 需要公共 key 注册，在 `PUBLIC_RESULT_DOWNLOAD_KEYS` 增加 `editor.jianying_draft_zip`，并在 `RESULT_OUTPUT_SPECS` 增加 `("editor.jianying_draft_zip", "jianying_draft")`。
- Express 直接下载白名单保持默认不开放：不要把 `editor.jianying_draft_zip` 加入 `EXPRESS_ALLOWED_DOWNLOAD_KEYS`。
- 生成侧是主控：Express 默认不生成该 artifact；下载白名单只是防御线。Studio 模式在 artifact 存在且校验通过时开放下载。

Gateway：

- 第一阶段不接 R2，只走现有 Job API local passthrough。
- Phase 2 上线后观察 30 天。若 `editor.jianying_draft_zip` 月下载次数 > `publish.dubbed_video` 月下载次数的 30%，再把它纳入 `storage.backend_router` 的可选 R2 key；否则继续 local passthrough，避免为低频大文件提前扩大 R2 成本。R2 仍必须保持失败回退本地。

素材包：

- `gateway/materials_pack_common.py` 新增 item:

```python
"jianying_draft": ["editor.jianying_draft_zip"]
```

- materials availability 也要按 service mode 控制：Express 默认隐藏并拒绝 `jianying_draft`，Studio 在 artifact 存在时展示。

前端：

- `ResultMediaCard` 新增“剪映草稿”下载按钮，位置在“配音视频/配音音频/素材包”同一行。
- 按钮只在 `materials-availability` 或 result summary 显示 `jianying_draft=true` 时出现。
- 文案建议：`剪映草稿` / `下载后解压，用剪映打开草稿目录`。如果不想在界面解释过多，可以在 toast 或帮助弹层里说明。

### 5.6 文件级改动清单

第一阶段后端 PoC：

| 文件 | 改动 |
| --- | --- |
| `src/modules/output/jianying/*` | 新增真实剪映草稿 backend、request/result model、writer、validator |
| `src/modules/output/output_models.py` | `OutputRequest` 增加 `include_jianying_draft: bool = False`，并承接 `service_mode` 或等价策略字段 |
| `src/modules/output/output_dispatcher.py` | 注入并调用 `JianyingDraftBackend`；注册 `editor.jianying_draft_*` artifacts；仅当 `service_mode == "studio"` 时生成 |
| `src/modules/output/manifest_writer.py` | `primary_outputs.editor` 增加 `jianying_draft_zip` 和兼容报告路径 |
| `src/pipeline/process.py` | 在 `_dispatch_process_output_bundle()` 构造 `OutputRequest` 时读取 `AVT_ENABLE_JIANYING_DRAFT`，传入 `include_jianying_draft`，并传递可用的 `service_mode` |
| `requirements*.txt` 或可选 extras | 仅在确认后增加 optional `pyJianYingDraft` 依赖；默认 clean env 不强依赖 |

第二阶段交付面：

| 文件 | 改动 |
| --- | --- |
| `src/services/web_ui/constants.py` | `PUBLIC_RESULT_DOWNLOAD_KEYS` 增加 `editor.jianying_draft_zip`，仅 Studio 可用 |
| `src/services/jobs/read_surface.py` | `RESULT_OUTPUT_SPECS` 增加 `("editor.jianying_draft_zip", "jianying_draft")` |
| `gateway/materials_pack_common.py` | `ITEM_TO_ARTIFACT_KEYS` 增加 `jianying_draft` |
| `src/services/jobs/api.py` | materials availability 增加 `jianying_draft`；Express 白名单保持默认不开放；Express 请求该素材项时拒绝或隐藏 |
| `frontend-next/src/types/jobs.ts` | `DOWNLOADABLE_ARTIFACT_KEYS` 增加 `editor.jianying_draft_zip` |
| `frontend-next/src/lib/api/downloads.ts` | `MaterialsAvailability` 增加 `jianying_draft` |
| `frontend-next/src/components/workspace/ResultMediaCard.tsx` | 增加“剪映草稿”下载按钮和素材包选项 |

第三阶段可选存储扩展：

| 文件 | 改动 |
| --- | --- |
| `gateway/storage/backend_router.py` | 评估是否把 `editor.jianying_draft_zip` 加入可 R2 redirect 的 artifact key |
| `gateway/job_intercept.py` | 若接 R2，则增加对应 download redirect 分支和友好文件名派生 |

## 6. 验证矩阵

### 6.1 自动化测试

新增单元测试：

- `tests/test_jianying_draft_backend.py`
  - 缺少 `source.original_video` 时 skipped，不影响 editor 产物。
  - 有视频、音频、字幕时生成 result，并注册 artifact。
  - `pyJianYingDraft` 未安装时跳过，不让 `pytest` 失败。

- `tests/test_output_dispatcher_jianying.py`
  - `include_jianying_draft=False` 时行为完全不变。
  - `include_jianying_draft=True` 时 manifest 包含新 artifact keys。

- `tests/test_job_api_jianying_download.py`
  - Studio 任务可下载 `editor.jianying_draft_zip`。
  - Express 任务默认不能下载，除非白名单明确变更。

- `tests/test_materials_pack_jianying.py`
  - 选中 `jianying_draft` 时 zip 可被素材包纳入。

### 6.2 手工兼容验证

必须用真实剪映桌面端验证：

| 剪映版本 | 平台 | 验证项 |
| --- | --- | --- |
| 5.9 | Windows | 草稿出现在草稿列表；时间线有原视频、配音、字幕；素材不丢失 |
| 6.x | Windows | 从零生成草稿是否可打开；不验证模板读取 |
| 7.x | Windows | 草稿是否可打开；不验证自动导出 |

每个版本至少验证：

- 关闭剪映后解压草稿到草稿目录。
- 重启或刷新剪映草稿列表。
- 打开草稿不崩溃、不空白。
- 原视频轨可见。
- 配音音轨与时间轴起点对齐。
- 字幕文本可编辑，时间码基本匹配。
- 素材路径移动后不会全部丢失，或报告里明确要求原素材路径存在。

## 7. 分阶段实施

### Phase 0: 样例采集与技术 spike

目标：

- 与字幕流程 v2 Phase 1a 并行启动，不要求 canonical cue gate 已完成。
- 用现有 SRT 先验证 `pyJianYingDraft`、草稿目录结构和剪映 5.9/6.x/7.x 桌面端可打开性。
- 在本地 Windows 剪映专业版上创建一个最小样例：原视频 + WAV 音频 + SRT 字幕。
- 对比样例草稿与 `pyJianYingDraft` 输出结构。
- 确认草稿目录名、封面、meta 文件、素材路径规则。

交付：

- `docs/research/jianying-draft-format-notes.md`
- 一个不进入主链的实验脚本，例如 `scripts/dev_generate_jianying_draft_poc.py`

### Phase 1: 后端 PoC

目标：

- 新增 `JianyingDraftBackend`。
- 在 `OutputDispatcher` 后接入，但默认关闭。
- 注册 `editor.jianying_draft_*` artifact。
- 生成 zip。
- 只消费 canonical `SubtitleCue`，不重新切字幕。
- 以字幕 v2 gate 为进入主链的前置条件；未满足时只记录 skipped reason。

验收：

- `pytest` 在未安装 `pyJianYingDraft` 的 clean env 仍通过。
- 开启开关并安装依赖后，能生成 zip。
- 手工解压后，剪映 5.9/6.x 至少一个版本可打开。
- 草稿字幕、下载 SRT 与 `subtitle_cues.json` 文本和时间一致。

### Phase 2: 交付面接入

目标：

- Job API 允许 Studio 下载 `editor.jianying_draft_zip`。
- 前端展示“剪映草稿”按钮。
- 素材包支持包含草稿 zip。

验收：

- 成功任务 result summary 能看到 `jianying_draft`。
- 点击按钮下载 zip。
- Express/Studio 白名单行为符合预期。

### Phase 3: 稳定性与版本矩阵

目标：

- 增加兼容报告。
- 增加版本/引擎标记。
- 对无法生成的输入类型给出明确 skipped reason。
- 决定是否将 `include_jianying_draft` 设为 Studio 默认开启。

验收：

- 每次生成的草稿 zip 内包含兼容报告。
- 不同剪映版本的打开结果可追踪。
- 失败不影响 `publish.dubbed_video` 和现有素材包。

## 8. 风险与缓解

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| 剪映私有格式变动 | 新版本打不开草稿 | 版本矩阵验证；兼容报告；adapter 边界可替换 |
| `pyJianYingDraft` 依赖不稳定 | clean env / CI 失败 | optional dependency；未安装时 skip |
| 素材路径丢失 | 用户打开草稿提示重连素材 | zip 内尽量包含资源副本；报告说明草稿目录必须整体解压 |
| 草稿 zip 过大 | 下载慢 / R2 成本上升 | 先 local passthrough；上线 30 天后若下载次数超过 `publish.dubbed_video` 的 30% 再接 R2 |
| 前端误导为“导入现有项目” | 用户预期错误 | 文案统一为“可打开的新草稿” |
| Express/Studio 权限混淆 | 暴露过多编辑资产 | 默认仅 Studio 开放，Express 继续只给成品视频 |
| 草稿配音轨为整轨，无法在剪映内分段 re-TTS | 用户期望“剪映里改字 + 自动重生成配音”落空 | §9 文案明确“字幕在剪映改，配音回平台改”；未来如需逐段编辑，再考虑输出逐段 wav 注册为多个音频片段 |

## 9. 建议的最终用户口径

中文产品文案：

- 按钮：`剪映草稿`
- 下载提示：`下载后解压，将草稿文件夹放入剪映草稿目录，再在剪映中打开。`
- 编辑边界：`字幕可在剪映直接编辑；如需修改配音，请回到本平台的修改流程。`
- 失败提示：`当前任务暂未生成剪映草稿，可下载配音视频或素材包继续编辑。`

避免文案：

- `导入剪映工程`
- `导入到当前项目`
- `自动发布到剪映`

这些说法会暗示官方导入能力或自动导出能力，和实际技术边界不一致。

## 10. 推荐下一步

并行推进字幕 v2 Phase 1a 与本方案 Phase 0，不直接改前端默认交付。也就是：

1. 启动字幕 v2 Phase 1a：实现 canonical `SubtitleCue` 最小路径，替换当前 editor SRT 二次切片主路径。
2. 同时启动剪映 Phase 0 spike：固定一个剪映版本，用现有 SRT 和 `pyJianYingDraft` 生成最小草稿，验证能否打开。
3. 完成 `subtitle_quality_report` hard error gate，阻断 text mismatch / timing overlap / timing_out_of_block / empty_cue。
4. 手工确认 spike 草稿可打开后，等待字幕 gate 达标，再把后端接入 `OutputDispatcher`，默认由环境变量关闭且 Studio-only。
5. 后端 PoC 只消费 canonical `SubtitleCue` 或由它生成的 SRT，不复用 spike 的旧 SRT 路径。
6. 只有当至少一个真实剪映版本验证通过，再进入前端下载按钮和素材包接入。

## 11. 架构修订:从主链自动生成改为按需生成 (2026-05-02)

> Status: Approved
> 取代 §5.4 的"OutputDispatcher 自动接入"架构

### 11.1 决策背景

phase 1 后端 PoC 完成后(commits 6b9ce6b..36921e2)发现一个产品决策:

- 一个剪映草稿 zip 包含原视频 + 配音 audio + 素材副本,**单 zip 体积 1-2 GB**
- **并非每个用户、每个任务都需要**剪映草稿;预估只有 30-50% 的 Studio 用户会用剪映继续编辑
- 任务跑完自动生成对所有任务一遍,**浪费磁盘 / IO / 未来 R2 成本**
- 用户主动点"生成剪映草稿"按钮是更清晰的知情同意路径

故 §5.4 的"主链自动生成"架构**改为按需触发**:

```
phase 1 PoC 路径(已废弃):
  视频翻译流程结束
  → OutputDispatcher 跑完编辑流程
  → 自动调用 JianyingDraftBackend
  → 生成 zip(1-2 GB,即使用户用不到)

新架构(本节):
  视频翻译流程结束
  → status="succeeded",result 页面显示"生成剪映草稿"按钮(仅 Studio 任务可见)
  → 用户点击按钮
  → API 触发后台 async 生成
  → status="running"  →  "succeeded"
  → 前端 polling 拿到状态变化
  → 显示"下载剪映草稿"按钮
  → 用户下载 zip
```

后端 backend 实现(§5.1-§5.3 的 `src/modules/output/jianying/*`)**100% 复用**,只是上层调用方式从 OutputDispatcher 改为 API 端点。

### 11.2 五个设计决策(2026-05-02 确认)

#### 11.2.1 status 字段命名

`jianying_draft_status: str` 持久化在 `JobRecord`,4 个状态:

| 值 | 含义 |
|---|------|
| `idle` | 初始状态 / 用户从未触发(任务首次完成默认即此值)|
| `running` | 后台正在生成 |
| `succeeded` | 生成完成,zip 可下载 |
| `failed` | 生成失败,允许重试 |

附加字段:
- `jianying_draft_error: str \| None` — failed 状态下的错误描述(给用户看 + 日志诊断)

#### 11.2.2 重复触发(API 幂等性)

`POST /jobs/{id}/generate-jianying-draft` 行为:

| 当前 status | 接口响应 |
|------------|---------|
| `idle` | **接受**,转 `running`,启动后台任务,返回 `202 Accepted` + `{status: "running"}` |
| `running` | **拒绝**,返回 `409 Conflict` + `{status: "running", message: "still in progress"}` |
| `succeeded` | **接受但不重新生成**,返回 `200 OK` + `{status: "succeeded", artifact_key: "editor.jianying_draft_zip"}` — 前端可立即 enable 下载 |
| `failed` | **接受**,清掉 error,转 `running`,启动新一轮 |

防止暴力点击触发并发生成。succeeded 直接复用,failed 允许重试。

#### 11.2.3 失败重试

允许**无限重试**(phase 1 不加 retry count 上限)。失败大概率是网络瞬断或 pyJianYingDraft 偶发 bug。如果 future 发现用户死磕同一失败任务搞乱日志,再加上限。

#### 11.2.4 过期清理

**不写单独的草稿清理逻辑**。草稿 zip 落在 `{project_dir}/jianying/exports/jianying_draft_*.zip`,job 自身的 cleanup 机制会一并清掉。

理由:保持系统简单,跟现有 cleanup pattern 一致,不引入新维护成本。

#### 11.2.5 Express 任务按钮可见性

**前端隐藏**(不渲染按钮),不是 disabled + tooltip:

```typescript
{job.serviceMode === "studio" && <GenerateJianyingButton ... />}
```

后端额外防御:`POST /jobs/{id}/generate-jianying-draft` 收到 `service_mode != "studio"` 的 job 时返回 `403 Forbidden`(防止 admin 工具或 curl 绕过前端)。

理由:Express 用户不会困惑"为什么按钮点了没用";marketing 转化(disabled + 升级提示)是产品决策,不在工程范围。

### 11.3 端到端数据流

```mermaid
sequenceDiagram
    autonumber
    participant U as User (Frontend)
    participant FE as ResultMediaCard
    participant GW as Gateway
    participant API as Job API
    participant BG as Background Worker
    participant BE as JianyingDraftBackend

    Note over U,FE: 任务 status=succeeded,Studio 模式,按钮可见
    U->>FE: 点击"生成剪映草稿"按钮
    FE->>GW: POST /job-api/jobs/{id}/generate-jianying-draft
    GW->>API: 透传(internal auth)
    API->>API: 检查 status,确认 idle/failed
    API->>API: status = "running"
    API->>BG: 提交后台任务(thread / asyncio)
    API-->>FE: 202 Accepted { status: "running" }
    FE->>FE: 启动 polling(usePollingTask hook,3s 间隔)
    BG->>BE: backend.write(JianyingDraftRequest)
    BE->>BE: writer + validator + zip
    BE-->>BG: JianyingDraftResult(ok / failed)
    BG->>API: 更新 status = "succeeded" / "failed"
    BG->>API: 注册 artifact (editor.jianying_draft_zip 等)
    Note over FE: polling 命中 status="succeeded"
    FE->>U: 显示"下载剪映草稿"按钮
    U->>FE: 点击下载
    FE->>GW: GET /job-api/jobs/{id}/download/editor.jianying_draft_zip
    GW->>API: 透传
    API-->>U: zip 字节流
```

### 11.4 API 端点 spec

#### `POST /job-api/jobs/{id}/generate-jianying-draft`

**触发草稿生成**。

Request body: 空(无需参数,所有信息从 job context 推导)。

Response:

```json
// 接受触发
{
  "status": "running",
  "started_at": "2026-05-02T15:00:00Z"
}

// 重复点击 — 已在跑
HTTP 409
{
  "status": "running",
  "message": "Jianying draft generation already in progress.",
  "started_at": "2026-05-02T14:58:00Z"
}

// 已生成 — 复用
{
  "status": "succeeded",
  "artifact_key": "editor.jianying_draft_zip",
  "draft_zip_path": "...",
  "completed_at": "2026-05-02T14:55:00Z"
}

// service_mode 不允许
HTTP 403
{
  "code": "service_mode_not_studio",
  "message": "Jianying draft is only available for Studio mode jobs."
}

// pyJianYingDraft 未装
HTTP 503
{
  "code": "engine_unavailable",
  "message": "Jianying engine is not available on this deployment."
}
```

#### `GET /job-api/jobs/{id}/jianying-draft-status`

**查状态**(前端 polling 用)。

Response:

```json
{
  "status": "idle" | "running" | "succeeded" | "failed",
  "started_at": "..." | null,
  "completed_at": "..." | null,
  "error": "..." | null,
  "artifact_key": "editor.jianying_draft_zip" | null,
  "draft_zip_size_bytes": 12345678 | null,
  "compatibility_report": { ... } | null
}
```

`artifact_key` 仅在 status="succeeded" 时设置,前端用来构造下载 URL。

### 11.5 JobRecord 字段扩展

`src/services/jobs/models.py` `JobRecord` 增加:

```python
@dataclass(slots=True)
class JobRecord:
    # ... 现有字段 ...

    # Jianying draft on-demand generation (plan §11)
    jianying_draft_status: str = "idle"  # idle / running / succeeded / failed
    jianying_draft_started_at: str | None = None  # ISO 8601 UTC
    jianying_draft_completed_at: str | None = None
    jianying_draft_error: str | None = None
```

JSON store 序列化默认值:`idle` / null / null / null。读取老数据时字段缺失,treat as `idle`。

跟 Codex 之前加 `service_mode` 字段是同样模式 — 已有先例。

### 11.6 后台异步执行策略

phase 1 第一版用**最简单的 threading**:

```python
# 在 Job API 端点内
def trigger_jianying_draft(job_id: str) -> dict:
    job = store.get(job_id)
    # ... gate checks ...
    job.jianying_draft_status = "running"
    job.jianying_draft_started_at = utc_now_iso()
    store.save(job)

    def _run_in_background():
        try:
            request = build_jianying_request_from_job(job)
            backend = JianyingDraftBackend()
            result = backend.write(request)
            # ... update status + register artifact ...
        except Exception as exc:
            # ... update status=failed + record error ...

    threading.Thread(target=_run_in_background, daemon=True).start()
    return {"status": "running", "started_at": ...}
```

不引入新的 worker 队列(Redis/Celery)— phase 1 PoC 阶段简化。如果 future 并发量大、需要可靠性(进程重启不丢任务),再引入持久化队列。

**进程重启风险**:Job API 重启时,`status="running"` 的草稿生成会"卡住"(线程死了,DB 还显示 running)。可加一个 startup 时的 reaper:启动时扫所有 `running` 超过 30 分钟的 jianying_draft_status,treat 为 failed(类似现有 `reap-stale` 机制)。这条留给 K3 task 实施。

### 11.7 K1-K10 任务拆分

#### K1: 回滚 phase 1 dispatcher 自动接入

回滚 J5/J6 在 OutputDispatcher / OutputRequest / process.py 的接入逻辑(后端模块 J1-J4 + manifest 字段 J7 保留):

- `src/modules/output/output_models.py`:移除 `OutputRequest.include_jianying_draft` / `service_mode` 字段(它们改为 API 端点参数语义,不是 dispatcher 参数)
- `src/pipeline/process.py`:移除 `AVT_ENABLE_JIANYING_DRAFT` env 读取
- `src/modules/output/output_dispatcher.py`:移除 `_maybe_generate_jianying_draft` / `_jianying_cue_gate_passes` / `_build_jianying_request` / `_register_jianying_artifacts` / `jianying_backend` 注入
- 删除对应测试 `tests/test_output_dispatcher_jianying.py` / `tests/test_output_request_jianying_fields.py` / `tests/test_process_dispatch_jianying_env.py`(完全废弃,不只是改)
- 保留 `tests/test_jianying_phase1_acceptance.py` 中**不依赖 dispatcher 的部分**(模块 importable + clean env smoke + subtitle consistency 仍然有意义),但移除 e2e 完整 dispatch 用例
- 保留 J7 的 manifest_writer 字段 + 测试(API 完成后写 manifest 也要用)

#### K2: JobRecord 加 jianying_draft_* 字段

- `src/services/jobs/models.py`:加 4 个字段
- 序列化 / 反序列化默认值
- 老数据读取兼容(字段缺失 → idle / null)
- 新测试:`tests/test_job_record_jianying_fields.py`

#### K3: 后台异步执行 + reap-stale 守护

- 新模块 `src/services/jobs/jianying_draft_runner.py`:封装 `trigger(job_id)` + 后台 thread + 状态更新
- 异常处理:writer 失败 → status=failed + error 记录
- 启动期 reaper:扫 running > 30min 的 jianying_draft_status 标 failed(类似 `reap_stale_jobs`)
- 测试:`tests/test_jianying_draft_runner.py`

#### K4: Job API `POST /generate-jianying-draft` 端点

- `src/services/jobs/api.py`:加路由
- gate 检查:status / service_mode
- 调用 K3 runner
- 测试:`tests/test_job_api_jianying_generate.py`

#### K5: Job API `GET /jianying-draft-status` 端点

- 加路由,返回 status + 时间戳 + error + artifact_key
- 测试:`tests/test_job_api_jianying_status.py`

#### K6: Gateway 路由转发

- `gateway/job_intercept.py` 或对应路由文件:把两个端点加入 internal proxy 白名单
- 测试:`tests/test_gateway_jianying_routes.py`

#### K7: 前端"生成剪映草稿"按钮

- `frontend-next/src/components/workspace/ResultMediaCard.tsx`:
  - service_mode === "studio" 时渲染按钮
  - 点击 → POST /generate-jianying-draft
  - 触发 polling(usePollingTask)
- API client `frontend-next/src/lib/api/jobs.ts`:加 generateJianyingDraft / getJianyingDraftStatus
- 测试:component 测试

#### K8: 前端下载按钮 + 状态展示

- 状态 `running`:按钮转"生成中..."loading
- 状态 `succeeded`:转"下载剪映草稿"按钮 + 文件大小 hint
- 状态 `failed`:转"重新生成" + error tooltip
- 状态 `idle`:初始按钮

#### K9: 集成测试

- `tests/test_jianying_on_demand_integration.py`:模拟用户触发 → polling → 下载完整流程
- e2e:用 mock Job API + real backend(importorskip)

#### K10: 部署到 US

- 改 Dockerfile 加 `pip install pyJianYingDraft`
- rebuild image(后台 nohup + log poll)
- DB migration(JobRecord 新字段)
- force-recreate app
- 验证按钮可见 + 触发流程

### 11.8 Rollback 路径

如果 K10 部署后发现 on-demand 流程有问题:

1. **快速 rollback** — 前端隐藏按钮(改 ResultMediaCard 加 `false &&` short-circuit),用户看不到入口,后端代码留着
2. **完全 rollback** — git revert K1-K9 的 commits + redeploy
3. **保留代码 + DB 字段 + 隐藏前端** — 最小损失

### 11.9 验收标准

- [ ] K10 部署后,Studio 任务的 result 页有"生成剪映草稿"按钮,Express 任务无
- [ ] 点击按钮 → 5-30 秒后 polling 转 succeeded → 下载按钮出现
- [ ] 下载的 zip 解压后能在剪映 10.5+ 打开
- [ ] 失败任务可重试,error 显示给用户
- [ ] 不影响 phase 1a 字幕 v2 主路径
- [ ] 不影响其他视频翻译流程
