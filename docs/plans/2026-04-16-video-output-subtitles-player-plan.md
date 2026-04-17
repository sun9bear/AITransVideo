# 视频输出优化：字幕 + 播放器 + 素材包 (V3-final)

> 日期：2026-04-16
> 状态：方案待实施（V3-final，Codex 三轮审核通过）
> 已完成前置改动：AlignedSegment 加 en_text + output_dispatcher 传递 en_text

---

## 1. 目标

当前 pipeline 产出了配音视频/音频/字幕，但存在三个问题：
1. 字幕只有中文、长句带标点，不符合短视频风格
2. 没有在线播放器，用户只能下载后查看
3. 下载区散列，缺少"素材包"概念

目标：剪映风格短句字幕 + workspace 页内嵌播放卡片 + 可选素材包下载。

---

## 2. Part 1: 字幕生成优化

### 2.1 AlignedSegment 加 en_text（✅ 已完成）

**文件**: `src/modules/output/editor/editor_package_models.py:16`

AlignedSegment 新增 `en_text: str` 字段（cn_text 之后）。

### 2.2 output_dispatcher 传递 en_text（✅ 已完成）

**文件**: `src/modules/output/output_dispatcher.py:127`

`_build_aligned_segments()` 从 `LocalizedProject.captions` 构建 `caption_map`，通过 `block.original_srt_indices` 回查每个 block 对应的 `en_text`，拼接后传入 AlignedSegment。

### 2.3 字幕数据模型：单一切片基准

**核心设计决策**（修复 V1 P1 问题）：

中英双语切分**必须基于同一组时间片**，不能各自独立切。否则 bilingual SRT 行数和时间轴不一致。

**切分策略**：以中文为主切，英文跟随对齐。

每个 AlignedSegment 生成一组 `SubtitleSlice`：

```python
@dataclass
class SubtitleSlice:
    start_ms: int
    end_ms: int
    zh_text: str   # 剪映风格短句，无标点
    en_text: str   # 对应英文短句，无标点
```

**切分流程**：
1. 对 `cn_text` 按中文标点 `，。！？；：、…` 切分为短句
2. 去掉所有标点
3. 超过 18 字的再按语义二次切
4. 合并 < 600ms 的短段
5. 时间按字数等比分配
6. 对 `en_text` 按相同分片数量做等比切分（按单词数），去标点
7. zh 和 en 的 slice 数量严格相等，时间轴共用

这样 3 种 SRT 共享同一组 `SubtitleSlice`：
- `subtitles_zh.srt` — 只取 `zh_text`
- `subtitles_en.srt` — 只取 `en_text`
- `subtitles_bilingual.srt` — 每个 cue 写两行（en_text\nzh_text）

### 2.4 重写 `_write_srt()` → 输出 3 个 SRT

**文件**: `src/modules/output/editor/editor_package_writer.py:229`

新逻辑：
1. 新增 `_build_subtitle_slices(segment) -> list[SubtitleSlice]`，替代现有 `_split_segment_into_subtitles()`
2. `_write_srt()` 生成 3 个文件
3. 保留原 `subtitles.srt` 作为 `subtitles_zh.srt` 的副本 → 兼容现有 `editor.subtitles` key

### 2.5 输出链路全程改动

现有输出链路只认识单个 `subtitles_path`，需全程打通：

| 文件 | 现状 | 改动 |
|------|------|------|
| `editor_package_models.py:41` | `ProjectOutputResult.subtitles_path: str` | 新增 `subtitles_en_path: str`、`subtitles_bilingual_path: str` |
| `editor_package_writer.py` | `_write_srt()` 写 1 个文件，返回 1 个路径 | 写 3 个文件，返回 3 个路径（zh 同时写为 `subtitles.srt` 兼容） |
| `output_dispatcher.py:229` | `_register_editor_artifacts()` 注册 `editor.subtitles` | 新增注册 `editor.subtitles_en`、`editor.subtitles_bilingual` |
| `manifest_writer.py:89` | `_build_primary_outputs()` 只有 `subtitles_path` | 新增 `subtitles_en_path`、`subtitles_bilingual_path` |
| `read_surface.py:19` | `RESULT_OUTPUT_SPECS` 只有 `editor.subtitles` | 新增 `editor.subtitles_en`、`editor.subtitles_bilingual` |
| `constants.py:27` | `PUBLIC_RESULT_DOWNLOAD_KEYS` | 新增 2 个字幕 key |
| `mappers.ts:25` | `downloadLabels` 只有 `editor.subtitles` | 新增中文标签 |
| `jobs.ts:88` | `DOWNLOADABLE_ARTIFACT_KEYS` | 新增 2 个 key |

原 `editor.subtitles` 保留指向 `subtitles.srt`（= zh 的副本），现有消费方零改动。

---

## 3. Part 2: 媒体流式播放

### 3.1 统一流端点（修复 V2-R2 P1：音频 fallback）

V2 只有 `stream/video`，但 audio fallback 的 `<audio>` 标签同样需要非 `Content-Disposition: attachment` 的流端点。统一为：

```
GET /job-api/jobs/{job_id}/stream/{kind}
```

| kind | artifact key | Content-Type |
|------|-------------|-------------|
| `video` | `publish.dubbed_video` | `video/mp4` |
| `audio` | `editor.dubbed_audio_complete` | `audio/wav` |

实现：在现有 `JobAPIHandler.do_GET()` 增加对 `stream/{kind}` subpath 的处理：
- 从 manifest 取对应 artifact path
- Range-aware 响应：解析 `Range` header → seek → 206 Partial Content
- 不设 `Content-Disposition: attachment`（区别于现有 download 端点）
- 其他 kind 值 → 404

**Gateway 代理问题**（修复 V2-R2 P1）：

现有 `proxy.py:92` 用 `upstream_response.content` 整体读入内存。对于 video/audio 流式播放，这意味着 Gateway 会缓冲整个文件。

**处理策略**：接受此代价并设体积上限。理由：
- 当前视频来源是 YouTube 片段，典型大小 50-200MB
- Gateway 服务器 2GB 内存，单次缓冲可承受
- 真正的流式转发需要改 httpx 为 `stream=True` + `StreamingResponse`，改动面大且影响所有代理路径
- 如果后续需要支持 >500MB 视频，再改 Gateway 代理为流式转发

### 3.2 为什么不复用现有下载端点

现有 `_write_binary()` 用 `read_bytes()` 一次读全文件到内存，不支持 Range 请求，且固定 `Content-Disposition: attachment`。浏览器 `<video>` / `<audio>` 标签需要 Range 支持才能 seek 和流式播放，且不能有 attachment disposition。

---

## 4. Part 3: 播放器 + 下载卡片

### 4.1 新组件 `ResultMediaCard`

**文件**: 新建 `frontend-next/src/components/workspace/ResultMediaCard.tsx`

布局（响应式卡片，嵌在 workspace 页面内，不全屏）：

```
桌面 (md+):
┌────────────────────────────────────────────┐
│ 翻译结果                                    │
├──────────────────────┬─────────────────────┤
│                      │ 下载                 │
│   <video> 播放器      │ ┌─────────────────┐ │
│   16:9 / 自适应       │ │ 配音视频    [↓]  │ │
│                      │ │ 配音音频    [↓]  │ │
│                      │ │ 素材包      [↓]  │ │
│                      │ └─────────────────┘ │
└──────────────────────┴─────────────────────┘

手机 (< md):
┌──────────────────────┐
│   <video> 播放器      │
│   16:9 / 全宽         │
├──────────────────────┤
│ 下载                  │
│ 配音视频         [↓]  │
│ 配音音频         [↓]  │
│ 素材包           [↓]  │
└──────────────────────┘
```

- 用现有 `Card` / `Button` 组件
- 播放器：原生 `<video controls>` 或 `<audio controls>`，`src` 指向 `/job-api/jobs/{jobId}/stream/{kind}`

### 4.2 播放器 fallback（修复 V1 P1 + V2-R2 P1）

当前前端 `output_target: 'editor'`（`jobs.ts:40`），很多完成的任务没有 `publish.dubbed_video`。

**fallback 策略**：
1. `publish.dubbed_video` 存在 → 显示 `<video>` 播放器，src = `stream/video`
2. 不存在但 `editor.dubbed_audio_complete` 存在 → 降级为 `<audio>` 播放器，src = `stream/audio`
3. 都不存在 → 不显示播放器区域，只显示下载列表

卡片组件接收 `materialsAvailability: MaterialsAvailability`（见 §4.3.1），根据可用性自适应布局。

### 4.3 素材包下载（弹窗可选）

#### 4.3.1 素材可用性端点（修复 V2-R2 P2）

前端需要知道哪些素材存在才能正确渲染勾选弹窗。新增：

```
GET /job-api/jobs/{job_id}/materials-availability
```

返回：
```json
{
  "source_video": true,
  "dubbed_video": true,
  "dubbed_audio": true,
  "segments": true,
  "subtitles_zh": true,
  "subtitles_en": true,
  "subtitles_bilingual": true
}
```

实现：遍历 artifact index 检查每个 key 对应文件是否存在，返回 boolean map。

前端类型：
```typescript
interface MaterialsAvailability {
  source_video: boolean
  dubbed_video: boolean
  dubbed_audio: boolean
  segments: boolean
  subtitles_zh: boolean
  subtitles_en: boolean
  subtitles_bilingual: boolean
}
```

`ResultMediaCard` 组件在挂载时调用此端点，用返回值同时驱动：
- 播放器 fallback（§4.2）
- 素材包弹窗中各选项的可选/不可选状态
- 下载按钮的可用性

#### 4.3.2 素材包弹窗

点击"素材包"按钮弹出 dialog，用户可勾选需要的资源：

| 可选项 | 后端 item key | artifact key | 说明 |
|--------|-------------|-------------|------|
| 原始视频 | `source_video` | `source.original_video` | 下载前的源视频 |
| 完整中文视频 | `dubbed_video` | `publish.dubbed_video` | 配音后的完整视频 |
| 完整中文音频 | `dubbed_audio` | `editor.dubbed_audio_complete` | 完整配音音频 |
| 分段音频包 | `segments` | `editor.segments_dir` | 各说话人各段的音频 |
| 字幕包 | `subtitles` | `editor.subtitles` + `_en` + `_bilingual` | 3 种字幕 |

不可用的项灰显且不可勾选（根据 `materialsAvailability`）。

**文件来源以 artifact key 为真源**（修复 V1 P2），通过 manifest / artifact index 解析实际路径，不硬编码目录。

#### 4.3.3 素材包端点

**决策**（修复 V2-R2 P1：Gateway 代理缓冲问题）：

**做成 Gateway-native 端点**，不走 Job API + 代理。理由：
- Gateway 已有 `project_dir` 信息（jobs DB），可直接读取文件系统
- 避免 proxy.py 的整体缓冲问题（大 zip 会撑爆 Gateway 内存）
- Gateway 可用 Starlette `StreamingResponse` 真正流式输出 zip

```
POST /api/jobs/{job_id}/materials-pack
Body: { "items": ["source_video", "dubbed_video", "dubbed_audio", "segments", "subtitles"] }
```

**文件**: 新建 `gateway/materials_api.py`

实现：
- 鉴权：`require_auth` + job 归属校验
- 从 Gateway DB 获取 `project_dir`
- 从 `project_dir` 读 manifest，解析 artifact key → 文件路径
- **实现方式**：`tempfile.SpooledTemporaryFile(max_size=10MB)` + `zipfile.ZipFile` 写入 → seek(0) → `StreamingResponse` 分块读取返回。**禁止使用 `BytesIO` 整包驻留内存**（现有 `api.py:119` 和 `handler.py:135` 的 tts-segments-zip 就是反例，素材包体积远大于 TTS 分段）。SpooledTemporaryFile 小于 10MB 走内存，超过自动落盘到临时文件，兼顾性能和内存安全。
- Content-Disposition: `materials_{job_id}.zip`
- 只打包用户勾选且实际存在的文件
- 体积上限：单个 zip 不超过 500MB，超过返回 413

### 4.4 替换 workspace 页面的下载区

**文件**: `frontend-next/src/app/(app)/workspace/[jobId]/page.tsx`

- 任务完成后，用 `ResultMediaCard` 替换现有 `ResultDownloadList`
- `ResultDownloadList` 保留但降级为折叠区（"更多下载"展开显示 manifest、translation.segments 等）

### 4.5 项目详情页对齐

**文件**: `frontend-next/src/app/(app)/projects/[jobId]/page.tsx:68`

项目详情页当前也使用 `ResultDownloadList`。两种方案：
- **推荐**：同步升级为 `ResultMediaCard`，保持两个结果页体验一致
- 备选：暂保留旧样式，后续统一

---

## 5. 改动文件清单

### 后端 Python

| 文件 | 类型 | 说明 |
|------|------|------|
| `src/modules/output/editor/editor_package_models.py` | 改 ✅ | AlignedSegment 加 en_text；ProjectOutputResult 加 2 个字幕路径 |
| `src/modules/output/output_dispatcher.py` | 改 ✅ + 改 | 传递 en_text（已完成）；`_register_editor_artifacts()` 注册新字幕 key |
| `src/modules/output/editor/editor_package_writer.py` | 改 | SubtitleSlice 模型 + 短句切分 + 3 SRT 输出 |
| `src/modules/output/manifest_writer.py` | 改 | `_build_primary_outputs()` 加字幕路径 |
| `src/services/jobs/read_surface.py` | 改 | `RESULT_OUTPUT_SPECS` 加 2 个字幕 key |
| `src/services/jobs/api.py` | 改 | 新增 `stream/{kind}` handler（Range 支持）+ `materials-availability` handler |
| `gateway/materials_api.py` | **新** | 素材包 Gateway-native 端点（SpooledTemporaryFile + StreamingResponse zip） |
| `gateway/main.py` | 改 | 注册 materials_api router |
| `src/services/web_ui/constants.py` | 改 | `PUBLIC_RESULT_DOWNLOAD_KEYS` 加 2 个 key |
| `src/services/jobs/api.py` | 改 | 新增 `stream/video` handler（Range 支持）+ `materials-pack` handler |

### 前端 Next.js

| 文件 | 类型 | 说明 |
|------|------|------|
| `frontend-next/src/components/workspace/ResultMediaCard.tsx` | **新** | 播放器+下载+素材包卡片 |
| `frontend-next/src/app/(app)/workspace/[jobId]/page.tsx` | 改 | 替换下载区为 ResultMediaCard |
| `frontend-next/src/app/(app)/projects/[jobId]/page.tsx` | 改 | 同步升级为 ResultMediaCard |
| `frontend-next/src/types/jobs.ts` | 改 | `DOWNLOADABLE_ARTIFACT_KEYS` 加 2 个字幕 key |
| `frontend-next/src/lib/api/mappers.ts` | 改 | `downloadLabels` 加新字幕标签 |
| `frontend-next/src/lib/api/downloads.ts` | 改 | 新增 stream URL builder + materials-pack URL builder |

---

## 6. 剪映草稿（暂缓）

pyJianYingDraft（3.1k stars）可生成剪映 5.x 草稿，但剪映 v6+ 加密了草稿文件，兼容性受限。当前降级为素材包下载，后续观察剪映版本情况再决定是否接入。

---

## 7. 验收与回归

### 手工验证

1. 跑一个任务到完成 → 检查 `output/` 下生成了 `subtitles.srt` + `subtitles_en.srt` + `subtitles_bilingual.srt`
2. SRT 内容：短句、无标点、3 个文件行数一致、bilingual 每个 cue 双行
3. 访问 workspace 页面 → 卡片显示播放器 + 3 个下载按钮
4. 有视频时：`<video>` 可播放、可 seek；无视频时：降级为 `<audio>` 或纯下载列表
5. 点击素材包 → dialog 弹窗可选资源 → 下载 zip 内容正确
6. 项目详情页 → 同样显示播放器卡片
7. 手机端访问 → 竖排布局正常
8. `npm run build` 通过

### pytest 回归（必跑命令）

```bash
python -m pytest \
  tests/test_output_dispatcher.py \
  tests/test_project_output.py \
  tests/test_manifest_writer.py \
  tests/test_job_read_surface.py \
  tests/test_job_api_phase1.py \
  tests/test_gateway_route_coverage.py \
  tests/test_web_ui.py \
  -q
```

| 测试文件 | 关注点 |
|---------|--------|
| `tests/test_output_dispatcher.py` | AlignedSegment 构造参数新增 en_text |
| `tests/test_project_output.py` | ProjectOutputResult 新增字幕路径字段 |
| `tests/test_manifest_writer.py` | manifest 注册新 artifact key |
| `tests/test_job_read_surface.py` | read_surface 新增字幕 spec |
| `tests/test_job_api_phase1.py` | 新 `stream/{kind}` + `materials-availability` subpath |
| `tests/test_gateway_route_coverage.py` | 路由枚举加入：素材包 `/api/jobs/{job_id}/materials-pack`（Gateway-native）；`stream/{kind}` 和 `materials-availability` 走 `intercept_job_subresource` 代理，加入 job subresource 参数表 |
| `tests/test_web_ui.py` | `PUBLIC_RESULT_DOWNLOAD_KEYS` 新增 2 个字幕 key |

---

## 8. Codex 审核修订记录

### R1（V1 → V2）

| # | V1 问题 | V2 修复 |
|---|---------|---------|
| P1 | 双语字幕 zh/en 独立切分，行数和时间轴不一致 | §2.3 定义 SubtitleSlice 单一切片基准，zh 主切 en 跟随 |
| P1 | 新字幕 artifact 改动面不全，manifest/read_surface/mappers 遗漏 | §2.5 列出输出链路全程 8 个文件改动 |
| P1 | 播放器假设任务必有视频，但 output_target=editor 时无视频 | §4.2 定义 3 级 fallback（video → audio → 纯下载） |
| P1 | 新端点 `/api/jobs/...` 平行长出第二套 API surface | §3.1/§4.3 改为 `/job-api/jobs/{job_id}/...`，沿用现有代理 |
| P2 | 素材包硬编码文件路径，不走 artifact index | §4.3 以 artifact key 为真源，manifest 解析实际路径 |
| P2 | 验证清单缺 pytest 回归 | §7 补充 6 个测试文件 |
| — | 项目详情页 ResultDownloadList 未同步升级 | §4.5 明确同步升级 |

### R2（V2 → V3）

| # | V2 问题 | V3 修复 |
|---|---------|---------|
| P1 | audio fallback 缺少非下载型流端点 | §3.1 统一为 `stream/{kind}` 覆盖 video + audio |
| P1 | materials-pack 流式 zip 会被 Gateway proxy 整体缓冲 | §4.3.3 素材包改为 Gateway-native 端点（StreamingResponse），不走 proxy |
| P2 | 前端弹窗不知道哪些素材存在/不存在 | §4.3.1 新增 `materials-availability` 端点返回 boolean map |
| P2 | pytest 命令不完整，遗漏 test_web_ui.py 和路由覆盖 | §7 补全为 7 个文件的完整命令 |

### R3（V3 收尾）

| # | V3 问题 | 修复 |
|---|---------|------|
| P2 | 流式 zip 实现方式未钉死，容易写回 BytesIO | §4.3.3 明确 `SpooledTemporaryFile(max_size=10MB)` + 分块读取，禁止 BytesIO |
| P2 | `stream/{kind}` 和 `materials-availability` 未纳入路由覆盖测试 | §7 明确加入 job subresource 参数表 |
| P2 | 文件清单 `api.py` 描述含 `materials-pack`，与 Gateway-native 决策冲突 | §5 改为仅 `stream/{kind}` + `materials-availability` |
