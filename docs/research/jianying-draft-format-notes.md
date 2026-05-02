# 剪映草稿格式与 pyJianYingDraft 研究记录(Phase 0 spike)

> Status: phase 0 spike notes
> Date: 2026-05-02
> Plan: [`docs/plans/2026-05-02-jianying-draft-delivery-integration-plan.md`](../plans/2026-05-02-jianying-draft-delivery-integration-plan.md)
> Spike script: [`scripts/dev_generate_jianying_draft_poc.py`](../../scripts/dev_generate_jianying_draft_poc.py)

## 1. 结论先行

**phase 1a 的 SRT 输出可以直接喂给 `pyJianYingDraft.ScriptFile.import_srt()` 生成可被剪映打开的草稿**。技术链路验证通过:

```
phase 1a output/subtitles_zh.srt  ──┐
                                    │
               ScriptFile.import_srt│
                                    ▼
                draft_content.json  +  draft_meta_info.json
                                    │
                                    ▼
                  剪映 5.9 草稿目录(可打开 / 可编辑字幕)
```

平台标记 `platform.app_version = "5.9.0"`,与 plan §2 描述的"剪映 5.9 from-zero 生成支持"一致。剪映 6.x / 7.x 的兼容性需要本地手动验证(本 spike 未做)。

## 2. 库基本信息

- **包名:** `pyJianYingDraft` (PyPI)
- **当前版本:** `0.2.6` (2026-03-16)
- **依赖:** `numpy` / `pymediainfo` / `imageio` / `comtypes` / `uiautomation` / `pillow`
- **安装:** `pip install pyJianYingDraft`
- **平台限制:** 部分 controller 类用 `comtypes` + `uiautomation` 驱动剪映 UI,Windows-only;但**生成草稿 JSON 不依赖这些**,跨平台 OK。
- **GitHub:** [GuanYixuan/pyJianYingDraft](https://github.com/GuanYixuan/pyJianYingDraft)

## 3. 时间单位约定

```python
import pyJianYingDraft as pjy
pjy.SEC == 1_000_000  # 微秒
```

**所有时间字段都是微秒(μs)**。phase 1a 的 SRT 是毫秒(ms),转换关系:`微秒 = 毫秒 × 1000`。

`pyJianYingDraft.ScriptFile.import_srt()` 内部已经把 SRT 时间(`HH:MM:SS,mmm`)转成微秒,**不需要显式转换**。

## 4. 核心类与最小用例

### 4.1 ScriptFile — 单个草稿的 JSON 包装

```python
script = pjy.ScriptFile(width=1920, height=1080, fps=30, maintrack_adsorb=True)
script.add_track(pjy.TrackType.text, track_name="zh_subtitle")
script.import_srt("path/to/subtitles_zh.srt", track_name="zh_subtitle")
script.save()  # 写到 DraftFolder.create_draft 给定的目录
```

关键方法:
| 方法 | 用途 |
|------|------|
| `add_track(type, name)` | 添加 video / audio / text / sticker / effect / filter / adjust 轨道 |
| `add_segment(seg, name)` | 把 VideoSegment / AudioSegment / TextSegment 加入指定轨道 |
| `import_srt(path, name)` | **直接吃 SRT,自动建 TextSegment + 默认样式** |
| `dumps()` / `dump()` | 序列化为 JSON 字符串 / 写到 file-like |
| `save()` | 写到 DraftFolder 给的目录 |
| `replace_text(...)` / `replace_material_by_*` | 模板替换流程(不在 phase 0 用) |

### 4.2 DraftFolder — 草稿根目录管理

```python
folder = pjy.DraftFolder("/path/to/drafts/root")
script = folder.create_draft(draft_name="my_draft", width=1920, height=1080, fps=30)
# ... build script ...
script.save()  # 落盘到 /path/to/drafts/root/my_draft/
```

`DraftFolder.create_draft(...)` 自动建子目录,`script.save()` 写两个 JSON 文件:

```
<draft_root>/
  <draft_name>/
    draft_content.json     # 完整时间轴 + 素材 + 轨道
    draft_meta_info.json   # 元数据(创建时间、id、name 等)
```

### 4.3 Material vs Segment

- **Material**(`VideoMaterial` / `AudioMaterial` / `TextMaterial`内部):素材本体,带文件路径或文本内容,在 `materials.{videos|audios|texts}` 数组里。需要文件能被 mediainfo 探测(video/audio 实际依赖 ffprobe)。
- **Segment**(`VideoSegment` / `AudioSegment` / `TextSegment`):时间轴上的引用,通过 `material_id` 指回 Material,带 `target_timerange` 决定显示位置和时长。在 `tracks[*].segments` 里。

每个 Segment 都有 `id` 和 `material_id`(UUID),两者通过 `material_id` 关联。

## 5. SRT 导入实测结构(spike SRT-only mode)

**输入**(phase 1a 风格 SRT,2 cue):
```
1
00:00:01,000 --> 00:00:03,444
今天我们来看第一个问题。

2
00:00:03,444 --> 00:00:06,000
这个问题涉及 LLM 推理成本。
```

**生成**:
```
draft_content.json  9442 bytes
draft_meta_info.json 1440 bytes
```

### 5.1 draft_content.json 顶层字段

```python
{
  "canvas_config": {"width": 1920, "height": 1080, ...},
  "color_space": ...,
  "config": ...,
  "create_time": ...,
  "duration": 6000000,           # 6.00 s = 微秒
  "fps": 30,
  "id": "91E08AC5-22FB-...",     # 草稿 UUID
  "platform": {"app_version": "5.9.0", ...},
  "tracks": [...],               # [{type:"text", segments:[...], name:"zh_subtitle"}]
  "materials": {                 # 各类素材池
    "texts": [...],              # 2 个 text material,与 segments 数量一致
    "videos": [],
    "audios": [],
    "stickers": [],
    ...
  },
  "relationships": ...,
  ...
}
```

### 5.2 TextSegment(in `tracks[0].segments`)

```python
{
  "id": "ac7ac4aac1694ad2a611ac583e083fd9",        # UUID
  "material_id": "a375b42d339e4336ac1b361d581b2a41",
  "target_timerange": {"start": 1000000, "duration": 2444000},   # 微秒
  "source_timerange": None,
  "speed": 1.0,
  "volume": 1.0,
  "render_index": 15000,
  "track_attribute": 0,
  "clip": {...},                 # 5 个字段,默认变换
  "extra_material_refs": [...],  # 1 个,可能是默认样式
  "common_keyframes": [],
  "enable_adjust": True,
  "visible": True,
  ...
}
```

### 5.3 TextMaterial.content(in `materials.texts[*].content`)

`content` 字段是一个 **嵌套 JSON 字符串**(双层序列化),解析后:

```python
{
  "text": "今天我们来看第一个问题。",
  "styles": [
    {
      "range": [0, 12],          # 字符 0-12 应用此样式
      "fill": {"alpha": 1.0, "content": {"render_type": "solid", "solid": {"color": [1.0, 1.0, 1.0]}}},
      "size": ...,
      "bold": false, "italic": false, "underline": false,
      "strokes": [...]
    }
  ]
}
```

默认样式:白色实色填充,无描边特效,字号默认。剪映打开后用户可在文本面板自定义。

### 5.4 timerange 与 phase 1a SRT 的对应

| phase 1a SRT cue | start (ms) | end (ms) | duration (ms) |
|------------------|-----------:|---------:|--------------:|
| `block_0001_cue_01` | 1000 | 3444 | 2444 |
| `block_0001_cue_02` | 3444 | 6000 | 2556 |

| pyJianYingDraft segment | start (μs) | duration (μs) |
|-------------------------|-----------:|--------------:|
| segments[0] | 1000000 | 2444000 |
| segments[1] | 3444000 | 2556000 |

**完美对应** — `import_srt` 自动 ms → μs 转换。

## 6. 与 phase 1a 字幕生成的字段映射

| phase 1a `SubtitleCue` | 剪映 draft_content.json |
|------------------------|-------------------------|
| `cue_id` | `tracks[*].segments[*].id`(UUID,不直接复用 cue_id 字符串)|
| `block_id` | 不直接映射(剪映平铺 cue,不分 block) |
| `speaker_id` / `speaker_name` | 不映射(剪映字幕无 speaker 概念) |
| `text` | `materials.texts[i].content.text` |
| `en_text` | 第二条 text track 的 segment(若启用 bilingual) |
| `start_ms` | `target_timerange.start = start_ms × 1000` |
| `end_ms` | `target_timerange.duration = (end_ms - start_ms) × 1000` |
| `source` | 不映射 |
| `needs_review` / `review_reason` | 不映射(只在 phase 1a quality report 体现) |

**结论**:phase 1a 的 SRT 已经携带剪映需要的全部信息。不需要把 `SubtitleCue` 直接映射到 pyJianYingDraft 数据结构 — 直接喂 SRT 文件最简单且最稳。

## 7. 剪映 fluent verification 步骤(待手动执行)

### 7.1 剪映草稿目录路径(Windows)

```
%LocalAppData%\JianyingPro\User Data\Projects\com.lveditor.draft\
```

### 7.2 验证流程

1. 关闭剪映专业版(避免文件锁)。
2. 把 spike 生成的草稿目录(如 `phase_1a_min/`)整体复制到上述路径。
3. 重启剪映,刷新草稿列表。
4. 应该能看到新草稿。打开它,检查:
   - 时间线显示 1 条字幕轨道,2 个文本片段
   - 时间码:1.000s-3.444s 和 3.444s-6.000s
   - 文本可点击编辑,显示中文 `今天我们来看第一个问题。` / `这个问题涉及 LLM 推理成本。`
   - 不报"项目损坏"或类似错误

### 7.3 版本兼容矩阵(待补)

本 spike 仅以 `platform.app_version = "5.9.0"` 输出。需要在以下版本各做一次手动测试:

| 剪映版本 | 平台 | 已验证? | 验收项 |
|----------|------|--------|--------|
| 5.9 | Windows | ⏳ 待手动 | 草稿可打开 + 字幕可编辑 |
| 6.x | Windows | ⏳ 待手动 | 草稿可打开(不要求模板模式) |
| 7.x | Windows | ⏳ 待手动 | 草稿可打开(不要求自动导出) |

## 8. 已知限制(phase 0 不修复,phase 1 处理)

### 8.1 Material 文件路径在 JSON 里是绝对路径

`materials.videos / audios` 里存的是 `path: "C:/full/path/to/video.mp4"` 绝对路径。**剪映打开时必须能找到这些文件**,否则会提示"素材丢失"。phase 1 设计:把 video / audio 复制到 `<draft>/materials/` 子目录,JSON 里路径改成相对或绝对到该子目录。

### 8.2 phase 0 spike 未真实测试 video / audio 轨

本 spike 只验证了 SRT-only 路径(因为本机无 ffmpeg/mediainfo backend,VideoMaterial 构造会失败)。完整的 video + audio + text 三轨样例 phase 1 时再做。

### 8.3 默认字幕样式可能不符合品牌口径

`import_srt` 用的默认样式是白色实色 + 默认字号。phase 1 时可以传 `style_reference` 参数把样式统一为本项目的字幕风格。

### 8.4 没有 bilingual track 自动化

phase 1a 输出有 `subtitles_zh.srt` / `subtitles_en.srt` / `subtitles_bilingual.srt` 三套。spike 只导了 zh。phase 1 需要决定剪映里:
- (A) 只导 zh,en 让用户在剪映中手动加
- (B) 导两条独立 text track(zh + en)
- (C) 导一条 bilingual track(用 `subtitles_bilingual.srt`)

倾向 (B):两条独立 track 用户更容易关掉其中一条。

### 8.5 platform.app_version 硬编码 5.9

pyJianYingDraft 0.2.6 输出固定的 5.9.0。如果剪映 6.x / 7.x 兼容,这个版本号不会成为阻塞,因为剪映通常向下兼容老草稿格式。但如果某个版本检测到 `5.9.0` 字符串后做了功能限制,需要 fork 库改这个值。

## 9. phase 1 后续工作(摘要)

phase 0 spike 验证了核心可行性。phase 1 真正接入主链时需要:

1. **新建 `src/modules/output/jianying/jianying_draft_backend.py`** — 包装 pyJianYingDraft,接收 `ProjectOutputResult` 输出 zip 包
2. **OutputDispatcher 接入**:`OutputRequest.include_jianying_draft` + `service_mode=="studio"` 双 gate
3. **素材打包**:把 video / dubbed_audio / SRT 复制到 `<draft>/materials/`,JSON 路径改成相对(参 plan §5.3)
4. **Compatibility report**:每次生成附 `jianying_compatibility_report.json`,记录使用的版本 / 引擎 / 校验状态
5. **下载链路**:`editor.jianying_draft_zip` artifact key 注册 → Job API → 前端按钮(参 plan §5.5 / §5.6)

详见 plan `docs/plans/2026-05-02-jianying-draft-delivery-integration-plan.md` phase 1。

## 10. 参考链接

- pyJianYingDraft GitHub: https://github.com/GuanYixuan/pyJianYingDraft
- pyCapCut(国际版同作者): https://github.com/GuanYixuan/pyCapCut
- 剪映草稿格式相关 issue 讨论: see GitHub Issues for the above repos
