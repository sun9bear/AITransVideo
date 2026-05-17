# GitNexus 编辑 / 后处理图

关联总图：`docs/graphs/GITNEXUS_PROJECT_GRAPH.md`

## 1. 范围

这张子图聚焦 `editing` 状态下的修改、重生成、speaker 生命周期、提交与 lineage 行为，重点是：

- editing speakers registry
- speaker voice profile inference
- `preview-source` cache 与 stream endpoint
- single segment re-TTS 与 batch re-TTS
- `overwrite / copy_as_new`
- `editing_audio_sync_required`
- Smart job 是否允许进入 editing
- Smart/Studio 项目列表“修改”入口
- editing 内音色复用、显式 clone、审听与 post-edit re-synthesis 计量

## 2. 主图

```mermaid
graph TD
    Projects["projects/page.tsx ResultMediaCard"] --> EditHref["editHref for studio/smart eligible jobs"]
    EditHref --> EditEntry["enter-edit"]
    EditEntry --> ModeGate["service_mode + smart_state editable gate"]
    ModeGate --> EditPage["VideoEditPage"]
    EditPage --> Segments["editing/segments.json + voice_map.json"]
    EditPage --> Speakers["editor/editing/speakers.json"]
    EditPage --> VoiceModify["VoiceModifyTab"]
    VoiceModify --> ReuseMatch["matchVoiceForSelection"]
    VoiceModify --> CloneModal["VoiceCloneModal reuse / explicit clone"]
    VoiceModify --> AudioAudit["SpeakerAudioAuditModal"]
    CloneModal --> VoiceOverride["apply voice override + voice_reuse"]

    Speakers --> CreateDialog["EditPageSpeakerCreateDialog"]
    CreateDialog --> CreateApi["POST /editing/speakers"]
    CreateApi --> SpeakersSvc["editing_speakers.py"]
    SpeakersSvc --> ProfileKick["maybe_trigger_inference(...)"]
    ProfileKick --> ProfileJob["editing_voice_profile.py"]
    ProfileJob --> ProfileBadge["EditPageSpeakerProfileBadge"]

    Segments --> SingleTTS["segment regenerate / draft wav"]
    Segments --> BatchTTS["editing_batch.py regenerate_all_dirty_segments"]
    Segments --> PreviewSource["preview-source cache + stream endpoint"]
    Segments --> Structural["split / speaker / text edits"]

    VoiceOverride --> SingleTTS
    Structural --> Audit["user_edit_events.jsonl"]
    SingleTTS --> Audit
    SingleTTS --> Usage["UsageMeter post_edit_resynth bucket"]
    BatchTTS --> StatusMap["segment_status.json"]
    StatusMap --> Audit
    BatchTTS --> Usage

    Audit --> Commit["editing_commit.py"]
    Commit --> SyncCheck["find text edits without regenerated TTS"]
    SyncCheck --> Guard["EditingAudioSyncRequiredError"]
    Commit --> Claim["update_job: editing -> running"]
    Claim --> Overwrite["overwrite -> resume alignment"]
    Claim --> CopyNew["copy_as_new -> new job lineage"]

    Overwrite --> Stamp["stamp tts_input_cn_text"]
    Overwrite --> Marker["effective_marker.marked_event_ids"]
    Overwrite --> Retire["clear stale jianying identity"]
    Stamp --> Reset["invalidate jianying draft + materials_pack"]
    Marker --> Reset
    Retire --> Reset
    Reset --> Resume["resume_from=alignment"]

    CopyNew --> Prepare["prepare_copy_project_dir / hardlinks / path rewrite"]
    Prepare --> NewJob["fresh job with clean deliverable state"]
```

## 3. 当前最大的变化

### 3.1 Smart job 进入 editing 需要二级状态门

- `src/services/jobs/api.py` 现在允许 `service_mode in {studio, smart}` 的任务进入 editing。
- 如果 `record.service_mode == "smart"`，还必须通过 `is_editable_smart_state(...)`。
- Smart 只有 `completed` 或 `downgraded_to_studio` 可编辑。
- `frontend-next/src/app/(app)/projects/page.tsx` 的 `EDITABLE_SERVICE_MODES` 同时包含 `studio` 和 `smart`，符合状态门的任务会在结果卡片上显示 edit href。

结论：Smart 审计身份可以保留，但 in-flight / refunded Smart job 不能进入 post-edit。

### 3.2 Editing 音色修改复用主审核组件

- `VoiceModifyTab.tsx` 复用 `VoiceCloneModal` 和 `SpeakerAudioAuditModal`，避免编辑页自建第二套 clone / 审听逻辑。
- modal 打开时同样查询 `voice-match`，可直接复用同源 UserVoice；需要新 clone 时仍要求用户显式点击。
- approve payload 可以带 `voice_reuse`，让 Gateway 区分复用已有音色与新克隆。
- clone 不是进入编辑页后的自动动作，仍受 clone lock、source metadata、calibration hook 约束。

结论：后编辑音色修改已经接回主审核流的复用/克隆安全边界，不做后台自动克隆。

### 3.3 editing speaker 仍然是独立实体

- `editing_speakers.py` 把编辑态 speakers 持久化到 `editor/editing/speakers.json`。
- 创建 speaker 通过 `file_lock(editing_speakers_path(project_dir))` 保护。
- 前端通过 `/editing/speakers` 读写，并通过 retry-profile 重新触发 profile 推断。

结论：editing speaker 是编辑态正式模型，不是临时 UI 字段。

### 3.4 batch re-TTS 只扫 dirty segment

- `src/services/jobs/editing_batch.py` 扫描 `segment_status.json`。
- 触发状态是 `text_dirty / voice_dirty / tts_failed`。
- `tts_loading` 和 `tts_dirty` 不会被批量覆盖。
- 单段失败不会中断整批，结果返回 succeeded/failed segment 列表。

结论：批量重合成是用户编辑后的显式处理面，不会自动覆盖用户尚未接受的 draft。

### 3.5 preview-source cache 继续作为独立回放侧路

- `POST /jobs/{jobId}/segments/{segmentId}/preview-source`
- `GET /job-api/jobs/{jobId}/segments/{segmentId}/preview-source-audio`
- 缓存落在 `editor/editing/preview_cache/{segment_id}.wav`

结论：编辑页继续区分“试听 draft TTS”和“回放原始分段音频”。

### 3.6 commit 仍然有 text/audio sync hard gate

- `_find_text_edits_without_tts(project_dir)` 检测文本改动但没有重新合成音频的 segment。
- 命中时抛 `EditingAudioSyncRequiredError`。
- 这条 gate 不替代 lineage / revision 冲突检查。

结论：post-edit text/audio sync 是提交硬约束。

### 3.7 overwrite 仍会主动退休旧交付物身份

- overwrite 会清空旧 `jianying_draft_attempt_id / substep / fingerprint`。
- 网关侧会调用 `invalidate_materials_pack_for_job(...)`。
- `edit_generation` 推进后，R2 交付也切到新的 generation key 空间。

结论：post-edit 后旧草稿、旧打包物、旧 R2 generation 都被视作 stale。

### 3.8 post-edit re-synthesis 有单独 usage bucket

- `UsageMeter` 增加 `TTS_BUCKET_POST_EDIT_RESYNTH`。
- post-edit 单段或批量重合成可以与主流程 TTS 分开统计 provider/model、字符数、调用次数和失败。
- 该数据进入成本/质量分析，但不改变 editing commit 的 text/audio sync 硬门。

结论：后编辑重合成已经具备成本归因入口，避免与主流水线 TTS 混算。

## 4. 关键证据

- `src/services/jobs/api.py`
  - Smart editable gate
  - editing endpoints
- `src/services/smart/state.py`
  - `is_editable_smart_state(...)`
- `frontend-next/src/app/(app)/projects/page.tsx`
  - Studio/Smart edit eligibility
- `frontend-next/src/app/(app)/workspace/[jobId]/edit/VoiceModifyTab.tsx`
  - Voice modify reuse / clone entry
- `frontend-next/src/components/workspace/VoiceSelectionPanel.tsx`
  - shared `VoiceCloneModal`
  - shared `SpeakerAudioAuditModal`
- `src/services/jobs/editing_batch.py`
  - dirty segment batch regenerate
- `src/services/jobs/editing_speakers.py`
  - speakers registry
- `src/services/jobs/editing_voice_profile.py`
  - fire-and-forget inference
- `src/services/jobs/editing_commit.py`
  - sync hard gate
  - overwrite claim
  - stale deliverable invalidation
- `src/services/usage_meter.py`
  - post-edit re-synthesis bucket

## 5. 什么时候优先看这张图

- 想改 Smart job 是否能进入 editing
- 想改项目列表是否展示“修改”入口
- 想改 editing 中音色复用、clone、审听入口
- 想改批量 re-TTS 或 segment status
- 想改 editing speakers 创建、profile 推断、retry-profile
- 想判断为什么某次 commit 报 `editing_audio_sync_required`
- 想改 post-edit 后交付物失效策略
