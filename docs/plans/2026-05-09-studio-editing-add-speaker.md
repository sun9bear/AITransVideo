# Studio Editing 模式新增说话人 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Studio 视频修改流程（`status=editing`）中允许用户新增 S2 漏检的说话人、把段落归属改到新说话人、并自动跑免费的 voice profile 推断（复用 admin 已配的 S2 Pass 3 多模态 LLM），不破坏 baseline 快照不变性。

**Architecture:** 在 `editor/editing/` 下新增 `speakers.json` 作为 editing-mode 说话人表（与 baseline 的 review_state.json speaker_review 解耦）。新增 `POST/GET /editing/speakers` 端点，并放开 `editing_segments.py` 的 "speaker 必须已在 segments 出现过" 卡口（line 491）以接受 editing/speakers.json 中已注册的新 speaker_id。Voice profile 走 fire-and-forget background task，复用 `transcript_reviewer.review_pass3_voice_profiles(mode="studio")`（自动选 admin 配置的模型）。前端两处入口：翻译修改 Tab dropdown 末尾 `+ 新增说话人...`、音色修改 Tab 顶部 `新增说话人` 按钮。

**Tech Stack:** Python (FastAPI gateway, stdlib HTTP Job API), Next.js / React / TypeScript / Tailwind, multimodal LLM via existing `transcript_reviewer` infrastructure.

---

## Decisions

### Locked

| ID | 决策 | 备注 |
|---|---|---|
| **D1** | display_name 同 job 内唯一；`strip()` + case-sensitive 比较 | 重复 → 409 给前端提示用户改名 |
| **D2** | speaker_id = 取 baseline 后下一个字母（`speaker_c` / `speaker_d` …） | 27+ 个时回退到 `speaker_<8字hex>` |
| **D3** | voice profile 模型从 `transcript_reviewer._get_prompt_model("studio", "pass3")` 取 | 即 admin 后台 S2 Pass 3 配置 |
| **D4** | 推断时机：fire-and-forget bg task；触发 = "speaker 第一次有 ≥1 段被归属" | 后续每次 reassign 不重复触发（除非 retry） |
| **D5** | 推断失败 → speakers.json `profile_status: "failed"` + `profile_error` | 前端显示"推断失败"+ 重试按钮，不阻塞预设选择 |
| **D6** | 新 speaker 默认 voice = null | 前端显示"未分配"，用户主动选预设或主动克隆（付费 user-initiated） |
| **D7** | commit overwrite：editing/speakers.json 内的新 speaker 写回 baseline `review_state.json::speaker_review.payload.speakers` | 同时 segments.json 落 baseline |
| **D8** | commit copy_as_new：editing/speakers.json 复制到新 job 的 review_state.json，源 job 不动 | 与现有 hardlink 策略一致 |
| **D9** | cancel：丢 editing/ 整目录，baseline 不动 | 现有行为，零改动 |
| **D10** | feature flag：复用 `AVT_ENABLE_POST_EDIT` env，与现有 editing 端点一起 gate | 默认 false |
| **D11** | 段 ↔ 源音频段 1:1 映射，改 segment.speaker_id 等同改源音频归属 | 不需要分两套 UI |
| **D12** | 所有后端 API 一律以 `project_dir` 为入参；editing 子目录走 `Path(project_dir) / "editor" / "editing"` 派生 | 与 `editing_segments.py` 现有 `patch_editing_segment(project_dir, ...)` 签名对齐 |
| **D13** | voice_profile 持久化进 `review_state.json` 的 `voice_selection_review` stage（不是 `speaker_review`） | display_name 走 `speaker_review.payload.speaker_names`，两者分开（与 web_ui/translation_review.py:146-153 一致） |
| **D14** | 前端 `EditingSpeaker` 类型加在 `frontend-next/src/lib/api/editing.ts` 内（与 `EditingSegment` 同位） | 不另开 `types/editing.ts`，与项目现有约定一致 |
| **D15** | 前端 fetch 一律走 `apiClient` 薄封装（`@/lib/api/client`），不裸 `fetch()` | 与 editing.ts 其他函数一致 |
| **D16** | 后台推断改用模块级 `ThreadPoolExecutor`（max_workers=2），测试可注入 dummy executor | 避免 daemon thread 在 tmp_path 清理后还在写文件 |

### Deferred to v2+

- 删除 speaker（低频，先观察）
- 修改 display_name（复用现有 review_state speaker_names 在线编辑机制即可）
- 多语言 display_name i18n
- 跨 job 的 speaker template / 复用

---

## File Structure

### Backend (5 new + 3 modified)

| 文件 | Status | 责任 |
|---|---|---|
| `src/services/jobs/editing_speakers.py` | new | speakers.json 读写、唯一性校验、speaker_id 生成 |
| `src/services/jobs/editing_voice_profile.py` | new | fire-and-forget 异步任务：拼源音频片段 → 调 `review_pass3_voice_profiles` |
| `tests/test_editing_speakers.py` | new | speakers.json CRUD + 唯一性 + ID 生成 |
| `tests/test_editing_segments_speaker_reassign.py` | new | PATCH 接受新 speaker_id；未注册仍拒绝 |
| `tests/test_editing_voice_profile_async.py` | new | 模型选择 + fail-soft + 触发条件 |
| `src/services/jobs/editing_segments.py` | modify | line 491 校验放开（接受 editing/speakers.json 中的 speaker_id） |
| `src/services/jobs/api.py` | modify | 加 `POST /editing/speakers` / `GET /editing/speakers` |
| `src/services/jobs/editing_commit.py` | modify | overwrite + copy_as_new 处理 editing/speakers.json |

### Gateway (1 modified)

| 文件 | Status | 责任 |
|---|---|---|
| `gateway/job_intercept.py` | modify | `_is_post_edit_mutation_subpath` 加 `/editing/speakers` 白名单 |

### Frontend (2 new + 2 modified)

| 文件 | Status | 责任 |
|---|---|---|
| `frontend-next/src/components/workspace/EditPageSpeakerCreateDialog.tsx` | new | 弹窗：display_name 输入 + 客户端去重 + 提交 |
| `frontend-next/src/components/workspace/EditPageSpeakerProfileBadge.tsx` | new | profile 状态徽章（pending / inferring / ready / failed） |
| `frontend-next/src/lib/api/editing.ts` | modify | 加 `EditingSpeaker` interface + `createEditingSpeaker` / `listEditingSpeakers` / `retryEditingSpeakerProfile` |
| `frontend-next/src/app/(app)/workspace/[jobId]/edit/page.tsx` | modify | 翻译修改 dropdown 末尾选项 + 音色修改 顶部按钮 + `NEXT_PUBLIC_ENABLE_POST_EDIT` gate |

---

## Schemas

### `editor/editing/speakers.json`

```json
{
  "version": 1,
  "speakers": [
    {
      "speaker_id": "speaker_c",
      "display_name": "桑达尔·皮查伊",
      "color": "#10B981",
      "source": "editing",
      "created_at": "2026-05-09T13:42:00Z",
      "profile_status": "ready",
      "profile_error": null,
      "voice_profile": {
        "voice_description": "成熟男性，温和...",
        "gender": "male",
        "age_group": "middle"
      }
    }
  ],
  "updated_at": "2026-05-09T13:45:00Z"
}
```

`profile_status` 取值：`"pending_segments"` (没段) / `"inferring"` (推断中) / `"ready"` / `"failed"`。
`source: "editing"` 与 baseline 的 `"baseline"` 区分；commit 时 baseline 不动，editing 的写回。

### POST `/editing/speakers` request / response

```json
// req
{ "display_name": "桑达尔·皮查伊" }

// 201
{ "speaker_id": "speaker_c", "display_name": "...", "profile_status": "pending_segments" }

// 409 (display_name 重复)
{ "error": "display_name_conflict", "message": "已存在同名说话人" }
```

### GET `/editing/speakers` response

```json
{
  "speakers": [
    /* baseline + editing 合并，按 created_at 升序 */
  ]
}
```

---

## Tasks

### Task 1: editing_speakers.py — 模块基础

**Files:**
- Create: `src/services/jobs/editing_speakers.py`
- Test: `tests/test_editing_speakers.py`

**契约（D12）**：所有公开 API 接收 `project_dir`（与 `patch_editing_segment` 一致），internal 派生 editing 目录。

- [ ] **Step 1: Write failing tests**

```python
# tests/test_editing_speakers.py
from __future__ import annotations
import re
from pathlib import Path
import pytest
from services.jobs.editing_speakers import (
    EditingSpeaker, DisplayNameConflictError,
    load_speakers, create_speaker, next_speaker_id, editing_speakers_path,
)


def _bootstrap_project(tmp_path: Path) -> Path:
    """Build a minimal project_dir with editor/editing/ subdir."""
    project = tmp_path / "project_xyz"
    (project / "editor" / "editing").mkdir(parents=True)
    return project


def test_speakers_path_under_editor_editing(tmp_path: Path) -> None:
    p = _bootstrap_project(tmp_path)
    assert editing_speakers_path(p) == p / "editor" / "editing" / "speakers.json"


def test_load_speakers_missing_file_returns_empty(tmp_path: Path) -> None:
    assert load_speakers(_bootstrap_project(tmp_path)) == []


def test_create_speaker_writes_file(tmp_path: Path) -> None:
    project = _bootstrap_project(tmp_path)
    sp = create_speaker(project, display_name="桑达尔", baseline_speakers=[])
    assert sp.speaker_id == "speaker_a"
    assert sp.display_name == "桑达尔"
    assert sp.source == "editing"
    assert sp.profile_status == "pending_segments"
    assert load_speakers(project)[0].speaker_id == "speaker_a"


def test_next_speaker_id_skips_baseline(tmp_path: Path) -> None:
    project = _bootstrap_project(tmp_path)
    baseline = [{"speaker_id": "speaker_a"}, {"speaker_id": "speaker_b"}]
    sp = create_speaker(project, display_name="C", baseline_speakers=baseline)
    assert sp.speaker_id == "speaker_c"


def test_display_name_conflict_within_editing(tmp_path: Path) -> None:
    project = _bootstrap_project(tmp_path)
    create_speaker(project, display_name="A", baseline_speakers=[])
    with pytest.raises(DisplayNameConflictError):
        create_speaker(project, display_name="A", baseline_speakers=[])


def test_display_name_conflict_against_baseline(tmp_path: Path) -> None:
    project = _bootstrap_project(tmp_path)
    baseline = [{"speaker_id": "speaker_a", "display_name": "Demis"}]
    with pytest.raises(DisplayNameConflictError):
        create_speaker(project, display_name="Demis", baseline_speakers=baseline)


def test_display_name_strips_whitespace(tmp_path: Path) -> None:
    project = _bootstrap_project(tmp_path)
    create_speaker(project, display_name=" Demis ", baseline_speakers=[])
    with pytest.raises(DisplayNameConflictError):
        create_speaker(project, display_name="Demis", baseline_speakers=[])


def test_overflow_after_z_falls_back_to_hex(tmp_path: Path) -> None:
    project = _bootstrap_project(tmp_path)
    baseline = [{"speaker_id": f"speaker_{c}"} for c in "abcdefghijklmnopqrstuvwxyz"]
    sp = create_speaker(project, display_name="X", baseline_speakers=baseline)
    assert re.fullmatch(r"speaker_[0-9a-f]{8}", sp.speaker_id)
```

- [ ] **Step 2: Run tests, expect 7 fails**

```bash
python -m pytest tests/test_editing_speakers.py -v
# Expected: 7 failed (module not yet imported)
```

- [ ] **Step 3: Implement minimal module**

```python
# src/services/jobs/editing_speakers.py
"""Editing-mode speakers registry. Persisted at
``<project_dir>/editor/editing/speakers.json``. Decoupled from baseline
``review_state.json``; merged back into baseline only at commit time."""
from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from services._file_lock import file_lock
from services.jobs.editing_segments import EDITING_SUBDIR_NAME

__all__ = [
    "EditingSpeaker", "DisplayNameConflictError",
    "load_speakers", "create_speaker", "next_speaker_id",
    "editing_speakers_path",
]

SPEAKERS_FILENAME = "speakers.json"
_PALETTE = (
    "#8B5CF6", "#06B6D4", "#10B981", "#F59E0B",
    "#EF4444", "#EC4899", "#6366F1", "#84CC16",
)


class DisplayNameConflictError(ValueError):
    """display_name 已存在（baseline 或 editing）。"""


@dataclass
class EditingSpeaker:
    speaker_id: str
    display_name: str
    color: str | None = None
    source: str = "editing"  # "baseline" | "editing"
    created_at: str = ""
    profile_status: str = "pending_segments"
    profile_error: str | None = None
    voice_profile: dict | None = None


def editing_speakers_path(project_dir: str | Path) -> Path:
    return Path(project_dir) / EDITING_SUBDIR_NAME / SPEAKERS_FILENAME


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _color_for_id(speaker_id: str) -> str:
    return _PALETTE[hash(speaker_id) % len(_PALETTE)]


def load_speakers(project_dir: str | Path) -> list[EditingSpeaker]:
    path = editing_speakers_path(project_dir)
    if not path.is_file():
        return []
    raw = json.loads(path.read_text("utf-8"))
    return [EditingSpeaker(**sp) for sp in raw.get("speakers", [])]


def _save(project_dir: str | Path, speakers: list[EditingSpeaker]) -> None:
    path = editing_speakers_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "speakers": [asdict(s) for s in speakers],
        "updated_at": _now_iso(),
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(path)


def next_speaker_id(used: Iterable[str]) -> str:
    used_set = set(used)
    for letter in "abcdefghijklmnopqrstuvwxyz":
        cand = f"speaker_{letter}"
        if cand not in used_set:
            return cand
    while True:
        cand = f"speaker_{secrets.token_hex(4)}"
        if cand not in used_set:
            return cand


def create_speaker(
    project_dir: str | Path,
    *,
    display_name: str,
    baseline_speakers: list[dict[str, Any]],
) -> EditingSpeaker:
    """Raises DisplayNameConflictError on duplicate (trim + case-sensitive)."""
    norm_name = display_name.strip()
    if not norm_name:
        raise ValueError("display_name must be non-empty")

    with file_lock(editing_speakers_path(project_dir)):
        existing = load_speakers(project_dir)
        all_names = {sp.display_name for sp in existing}
        for bl in baseline_speakers:
            bl_name = (bl.get("display_name") or "").strip()
            if bl_name:
                all_names.add(bl_name)
        if norm_name in all_names:
            raise DisplayNameConflictError(
                f"display_name {norm_name!r} already exists"
            )

        used_ids = {sp.speaker_id for sp in existing}
        for bl in baseline_speakers:
            bid = bl.get("speaker_id")
            if bid:
                used_ids.add(bid)

        new_sp = EditingSpeaker(
            speaker_id=next_speaker_id(used_ids),
            display_name=norm_name,
            color=None,  # set after id allocated below
            source="editing",
            created_at=_now_iso(),
            profile_status="pending_segments",
        )
        new_sp.color = _color_for_id(new_sp.speaker_id)
        existing.append(new_sp)
        _save(project_dir, existing)
        return new_sp
```

- [ ] **Step 4: Run tests, expect all pass**

```bash
python -m pytest tests/test_editing_speakers.py -v
# Expected: 7 passed
```

- [ ] **Step 5: Commit**

```bash
git add src/services/jobs/editing_speakers.py tests/test_editing_speakers.py
git commit -m "feat(editing): add editing-mode speakers.json registry"
```

---

### Task 2: PATCH segment update — relax speaker_id 校验

**Files:**
- Modify: `src/services/jobs/editing_segments.py` `_propagate_speaker_change()` （line ~491；该函数没有 `project_dir` 形参，需要从 caller 传入）
- Test: `tests/test_editing_segments_speaker_reassign.py`

**注意**：`_propagate_speaker_change` 当前只接收 `segments / index / updated / new_speaker_id`，没有 `project_dir`。要把卡口放开成"未在 segments 出现也允许，**只要在 editing/speakers.json 里注册过**"，需要把 `project_dir` 透传进去。改 `patch_editing_segment` 调用 `_propagate_speaker_change(project_dir=project_dir, ...)`。

- [ ] **Step 1: Write failing tests**

```python
# tests/test_editing_segments_speaker_reassign.py
import json
from pathlib import Path
import pytest
from services.jobs.editing_segments import patch_editing_segment
from services.jobs.editing_speakers import create_speaker


def _bootstrap_project(tmp_path: Path) -> Path:
    """project_dir 含 editor/editing/{segments,segment_status}.json，
    内有 speaker_a / speaker_b 两段。"""
    project = tmp_path / "project_xyz"
    edit_dir = project / "editor" / "editing"
    edit_dir.mkdir(parents=True)
    segments = [
        {"segment_id": "seg_1", "speaker_id": "speaker_a", "cn_text": "a"},
        {"segment_id": "seg_2", "speaker_id": "speaker_b", "cn_text": "b"},
    ]
    (edit_dir / "segments.json").write_text(
        json.dumps(segments, ensure_ascii=False), "utf-8"
    )
    (edit_dir / "segment_status.json").write_text("{}", "utf-8")
    return project


def test_reassign_to_registered_editing_speaker_succeeds(tmp_path: Path) -> None:
    project = _bootstrap_project(tmp_path)
    create_speaker(project, display_name="C", baseline_speakers=[])
    # editing/speakers.json 里有 speaker_a — 但 baseline=[] 时 next_speaker_id="speaker_a"。
    # 这里需要 baseline 和 segments 一致；改用 baseline 注入：
    sp = create_speaker(
        project, display_name="C2",
        baseline_speakers=[{"speaker_id": "speaker_a"}, {"speaker_id": "speaker_b"}],
    )
    assert sp.speaker_id == "speaker_c"
    result = patch_editing_segment(
        project, segment_id="seg_1", patch={"speaker_id": "speaker_c"}
    )
    assert result["speaker_id"] == "speaker_c"


def test_reassign_to_unknown_speaker_still_rejected(tmp_path: Path) -> None:
    project = _bootstrap_project(tmp_path)
    with pytest.raises(ValueError, match="not found"):
        patch_editing_segment(
            project, segment_id="seg_1", patch={"speaker_id": "speaker_z"}
        )


def test_reassign_to_baseline_speaker_still_works(tmp_path: Path) -> None:
    project = _bootstrap_project(tmp_path)
    result = patch_editing_segment(
        project, segment_id="seg_1", patch={"speaker_id": "speaker_b"}
    )
    assert result["speaker_id"] == "speaker_b"


def test_reassign_to_speaker_only_existing_in_speakers_json_with_no_segments(tmp_path: Path) -> None:
    """Regression for the no-op early-return path (line ~404):
    speaker_id 是唯一 patch 字段、值与 baseline 一致 → 早返。
    本测试确保新注册 speaker 不会因此被旁路。"""
    project = _bootstrap_project(tmp_path)
    create_speaker(
        project, display_name="C",
        baseline_speakers=[{"speaker_id": "speaker_a"}, {"speaker_id": "speaker_b"}],
    )
    # PATCH seg_1.speaker_id 从 speaker_a → speaker_c：必须经 _propagate_speaker_change
    result = patch_editing_segment(
        project, segment_id="seg_1", patch={"speaker_id": "speaker_c"}
    )
    assert result["speaker_id"] == "speaker_c"
```

- [ ] **Step 2: Run tests, expect 2 fail**

```bash
python -m pytest tests/test_editing_segments_speaker_reassign.py -v
# test_reassign_to_registered_editing_speaker_succeeds → FAIL
# test_reassign_to_speaker_only_existing_in_speakers_json_with_no_segments → FAIL
# (current code: "speaker 'speaker_c' not found in task")
```

- [ ] **Step 3: Patch the guard + propagate `project_dir` into `_propagate_speaker_change`**

```python
# src/services/jobs/editing_segments.py

# (caller change, around line 396-402)
if speaker_changed:
    _propagate_speaker_change(
        project_dir=project_dir,           # <-- new
        segments=segments,
        index=index,
        updated=updated,
        new_speaker_id=str(applied["speaker_id"]),
    )

# (signature change, line ~439)
def _propagate_speaker_change(
    *,
    project_dir: str | Path,               # <-- new
    segments: list[dict[str, Any]],
    index: int,
    updated: dict[str, Any],
    new_speaker_id: str,
) -> None:
    ...
    if new_speaker_id not in known_speakers:
        # 2026-05-09: editing/speakers.json may register a fresh speaker
        # that has no segments yet. Accept those IDs as the legitimate
        # "first segment assignment" path.
        from services.jobs.editing_speakers import load_speakers
        editing_ids = {sp.speaker_id for sp in load_speakers(project_dir)}
        if new_speaker_id not in editing_ids:
            raise ValueError(
                f"speaker {new_speaker_id!r} not found in task or editing "
                f"speakers; known: {sorted(known_speakers | editing_ids)}. "
                "Cannot reassign to an unknown speaker — no implicit creation."
            )
```

- [ ] **Step 4: Run tests, expect all 3 pass**

```bash
python -m pytest tests/test_editing_segments_speaker_reassign.py -v
# 3 passed
```

- [ ] **Step 5: Commit**

```bash
git add src/services/jobs/editing_segments.py tests/test_editing_segments_speaker_reassign.py
git commit -m "feat(editing): allow PATCH to assign editing-registered speakers"
```

---

### Task 3: Job API endpoints `POST/GET /editing/speakers`

**Files:**
- Modify: `src/services/jobs/api.py`
- Modify: `src/services/jobs/editing_speakers.py` — add `load_baseline_speakers(project_dir) -> list[dict]` helper（读 `review_state.json::speaker_review.payload.speaker_names`）
- Test: extend `tests/test_editing_speakers.py` with HTTP-level tests

**注意（Reviewer P1#2/#8）**：
1. Job API dispatch 用 inline `if/elif` against `path_parts`，参考现有 `path_parts[2] == "editing" and path_parts[3] == "voice-map"` 模式（api.py:896）
2. Baseline speakers 来源 = `review_state.json::stages.speaker_review.payload.speaker_names`（dict[speaker_id, display_name]），不是不存在的 `.speakers` 字段。需要在 `editing_speakers.py` 加 helper 读取并转成 `[{"speaker_id":..., "display_name":...}, ...]` 形态供 `create_speaker` 用
3. `record.project_dir` 是 JobRecord 字段（已存在），不要用不存在的 `service.editor_dir(job_id)`

- [ ] **Step 1: Add `load_baseline_speakers` helper + tests**

```python
# tests/test_editing_speakers.py — append
def test_load_baseline_speakers_reads_review_state(tmp_path: Path) -> None:
    project = _bootstrap_project(tmp_path)
    rs_dir = project / "review"
    rs_dir.mkdir(parents=True)
    (rs_dir / "review_state.json").write_text(json.dumps({
        "stages": {
            "speaker_review": {
                "payload": {
                    "speaker_names": {"speaker_a": "Demis", "speaker_b": "Gary"}
                }
            }
        }
    }), "utf-8")
    from services.jobs.editing_speakers import load_baseline_speakers
    bl = load_baseline_speakers(project)
    assert {"speaker_id": "speaker_a", "display_name": "Demis"} in bl
    assert {"speaker_id": "speaker_b", "display_name": "Gary"} in bl
```

```python
# src/services/jobs/editing_speakers.py — append
def load_baseline_speakers(project_dir: str | Path) -> list[dict]:
    """Read baseline display_names from review_state.json.

    Returns ``[{"speaker_id": "...", "display_name": "..."}, ...]`` — the
    schema this module expects. Uniqueness check + ID allocation rely on
    this. Returns ``[]`` if review_state is missing or malformed.
    """
    rs_path = Path(project_dir) / "review" / "review_state.json"
    if not rs_path.is_file():
        return []
    try:
        rs = json.loads(rs_path.read_text("utf-8"))
        names = (
            rs.get("stages", {})
              .get("speaker_review", {})
              .get("payload", {})
              .get("speaker_names", {})
        )
        if not isinstance(names, dict):
            return []
        return [
            {"speaker_id": str(sid), "display_name": str(dn)}
            for sid, dn in names.items()
        ]
    except (json.JSONDecodeError, OSError):
        return []
```

- [ ] **Step 2: HTTP-level tests** (uses existing `tests/conftest.py` fixtures — see how `test_editing_segments_*.py` boots Job API; mirror that pattern. Skip if you're not familiar with that fixture; rely on Step 1 unit tests + manual smoke for now)

- [ ] **Step 3: Wire endpoints into `api.py` (inline if/elif against path_parts)**

Locate `path_parts[2] == "editing"` blocks (around line 229 GET / line 896 POST). Add:

```python
# src/services/jobs/api.py — inside GET handler block, after the existing
# editing/segments + editing/voice-map handlers:
elif (
    method == "GET"
    and len(path_parts) == 4
    and path_parts[2] == "editing"
    and path_parts[3] == "speakers"
):
    record = self._service.store.get_record(job_id)
    if not record:
        self.send_error(404, "job not found")
        return
    from services.jobs.editing_speakers import (
        load_speakers, load_baseline_speakers,
    )
    from dataclasses import asdict
    baseline = load_baseline_speakers(record.project_dir)
    editing = load_speakers(record.project_dir)
    merged = [
        {**bl, "source": "baseline", "profile_status": "ready"}
        for bl in baseline
    ]
    merged.extend(asdict(s) for s in editing)
    self._write_json(200, {"speakers": merged})
    return

# inside POST handler block:
elif (
    method == "POST"
    and len(path_parts) == 4
    and path_parts[2] == "editing"
    and path_parts[3] == "speakers"
):
    record = self._service.store.get_record(job_id)
    if not record:
        self.send_error(404, "job not found")
        return
    if record.status != "editing":
        self._write_json(409, {"error": "job_not_in_editing"})
        return
    body = self._read_json_payload()  # api.py:1528 helper
    display_name = (body or {}).get("display_name", "")
    if not isinstance(display_name, str) or not display_name.strip():
        self._write_json(422, {"error": "display_name_required"})
        return
    from services.jobs.editing_speakers import (
        create_speaker, load_baseline_speakers, DisplayNameConflictError,
    )
    from dataclasses import asdict
    try:
        sp = create_speaker(
            record.project_dir,
            display_name=display_name,
            baseline_speakers=load_baseline_speakers(record.project_dir),
        )
    except DisplayNameConflictError:
        self._write_json(409, {
            "error": "display_name_conflict",
            "message": "已存在同名说话人",
        })
        return
    # D4: 不在创建时触发推断（speaker 还没段）。后续 segment PATCH 时触发。
    self._write_json(201, asdict(sp))
    return
```

调用前确认 `_read_json_body` / `_json_response` 是 handler 已有 helper 名（grep `def _json_response` / `def _read_json_body` 确认；如名字不同照搬现有 editing handler 的写法）。

- [ ] **Step 4: Run tests + commit**

```bash
python -m pytest tests/test_editing_speakers.py -v
git add src/services/jobs/api.py src/services/jobs/editing_speakers.py tests/test_editing_speakers.py
git commit -m "feat(editing): POST/GET /editing/speakers endpoints"
```

---

### Task 4: Gateway whitelist `editing/speakers`

**Files:**
- Modify: `gateway/job_intercept.py` `_POST_EDIT_SIMPLE_MUTATION_SUBPATHS` (frozenset, line 1879)

**注意（Reviewer P1#7）**：实际常量是 `_POST_EDIT_SIMPLE_MUTATION_SUBPATHS`（frozenset，不是 tuple；元素**不带前导斜杠**）。GET `/editing/speakers` 是只读，**不**走 mutation gate；feature flag 走 `_is_post_edit_mutation_subpath` 只对 POST 生效，GET 自然透传给 Job API（Job API 自己判 record + 拒访不属于自己的任务，已有逻辑）。

- [ ] **Step 1: Add unit test**

```python
# tests/test_phase1_guards.py — append
def test_editing_speakers_in_post_edit_simple_mutation_whitelist() -> None:
    from gateway.job_intercept import _POST_EDIT_SIMPLE_MUTATION_SUBPATHS
    assert "editing/speakers" in _POST_EDIT_SIMPLE_MUTATION_SUBPATHS

def test_editing_speakers_post_routed_as_mutation() -> None:
    from gateway.job_intercept import _is_post_edit_mutation_subpath
    assert _is_post_edit_mutation_subpath("editing/speakers") is True
```

- [ ] **Step 2: Run, expect fail**

- [ ] **Step 3: Add to frozenset**

```python
# gateway/job_intercept.py:1879
_POST_EDIT_SIMPLE_MUTATION_SUBPATHS: frozenset[str] = frozenset({
    "regenerate-all-tts",
    "regenerate-all-tts/cancel",
    "editing/voice-map",
    "editing/revert-unsynced-text",
    "editing/speakers",  # 2026-05-09: studio editing add-speaker plan
})
```

- [ ] **Step 4: Run, expect pass + commit**

```bash
git add gateway/job_intercept.py tests/test_phase1_guards.py
git commit -m "feat(gateway): whitelist editing/speakers as post-edit mutation"
```

---

### Task 5: Voice profile background task

**Files:**
- Create: `src/services/jobs/editing_voice_profile.py`
- Test: `tests/test_editing_voice_profile_async.py`
- Modify: `src/services/jobs/editing_segments.py` — call `maybe_trigger_inference` after speaker_id reassignment

**注意（Reviewer P1#4/#5/#6 + D16）**：
- `review_pass3_voice_profiles` 真实签名（transcript_reviewer.py:1912）：`(lines, *, source_audio_path, speakers, video_title="", mode="studio", ...)`，`lines` 是 transcript 行数组（每条含 `start_ms`/`end_ms`/`speaker_id`/`text`），`source_audio_path` 一般是 `<project_dir>/audio/source.<ext>` 或 transcript 阶段的输入 wav；要先读 `manifest.json` / `record` 拿确切路径
- `speakers` dict shape 见 `transcript_reviewer.py:1924`：`{speaker_id: {voice_description?, gender?, age_group?, ...}}`，新 speaker 传 `{speaker_id: {}}` 即可
- `lines` **必须包含至少一条** `speaker_id == new speaker_id` 的，否则 `_extract_speaker_audio_clips` 没有素材（D5 fail-soft：返 `{...failed}` 而不是炸）
- 后台跑改用模块级 `ThreadPoolExecutor(max_workers=2)`（D16），可注入 dummy executor 让测试同步跑

- [ ] **Step 1: Write failing tests**

```python
# tests/test_editing_voice_profile_async.py
import json
from pathlib import Path
from concurrent.futures import Future
from unittest.mock import patch, MagicMock
import pytest
from services.jobs.editing_speakers import create_speaker, load_speakers


def _bootstrap_project_with_segments(tmp_path: Path, *, with_speaker_a_segment=True) -> Path:
    project = tmp_path / "project_xyz"
    edit_dir = project / "editor" / "editing"
    edit_dir.mkdir(parents=True)
    (project / "audio").mkdir()
    audio = project / "audio" / "source.wav"
    audio.write_bytes(b"RIFFmock-wav")  # placeholder; real test uses mocked p3
    segments = []
    if with_speaker_a_segment:
        segments.append({
            "segment_id": "seg_1",
            "speaker_id": "speaker_a",
            "start_ms": 0, "end_ms": 5000,
            "source_text": "hello world",
        })
    (edit_dir / "segments.json").write_text(
        json.dumps(segments, ensure_ascii=False), "utf-8"
    )
    (project / "manifest.json").write_text(
        json.dumps({"audio_source_path": str(audio)}), "utf-8"
    )
    return project


def test_inference_uses_studio_mode(tmp_path):
    project = _bootstrap_project_with_segments(tmp_path)
    create_speaker(project, display_name="C", baseline_speakers=[])
    from services.jobs.editing_voice_profile import infer_voice_profile_for_speaker
    with patch(
        "services.jobs.editing_voice_profile.review_pass3_voice_profiles",
        return_value={"speaker_a": {"voice_description": "warm"}},
    ) as mock_p3:
        infer_voice_profile_for_speaker(project, "speaker_a")
    assert mock_p3.call_args.kwargs["mode"] == "studio"


def test_inference_failure_is_fail_soft_not_raised(tmp_path):
    project = _bootstrap_project_with_segments(tmp_path)
    create_speaker(project, display_name="C", baseline_speakers=[])
    from services.jobs.editing_voice_profile import infer_voice_profile_for_speaker
    with patch(
        "services.jobs.editing_voice_profile.review_pass3_voice_profiles",
        side_effect=RuntimeError("LLM down"),
    ):
        infer_voice_profile_for_speaker(project, "speaker_a")  # MUST NOT raise
    sp = next(s for s in load_speakers(project) if s.speaker_id == "speaker_a")
    assert sp.profile_status == "failed"
    assert "LLM down" in (sp.profile_error or "")


def test_inference_success_writes_profile_and_status_ready(tmp_path):
    project = _bootstrap_project_with_segments(tmp_path)
    create_speaker(project, display_name="C", baseline_speakers=[])
    from services.jobs.editing_voice_profile import infer_voice_profile_for_speaker
    with patch(
        "services.jobs.editing_voice_profile.review_pass3_voice_profiles",
        return_value={"speaker_a": {"voice_description": "warm", "gender": "male"}},
    ):
        infer_voice_profile_for_speaker(project, "speaker_a")
    sp = next(s for s in load_speakers(project) if s.speaker_id == "speaker_a")
    assert sp.profile_status == "ready"
    assert sp.voice_profile == {"voice_description": "warm", "gender": "male"}


def test_maybe_trigger_idempotent_skips_when_not_pending(tmp_path, monkeypatch):
    project = _bootstrap_project_with_segments(tmp_path)
    create_speaker(project, display_name="C", baseline_speakers=[])
    # Manually flip status to 'inferring'
    from services.jobs.editing_voice_profile import (
        maybe_trigger_inference, _update_speaker_status,
    )
    _update_speaker_status(project, "speaker_a", status="inferring")

    submit_calls = []
    class _DummyExecutor:
        def submit(self, fn, *args, **kw):
            submit_calls.append((fn, args, kw))
            f = Future(); f.set_result(None); return f
    from services.jobs import editing_voice_profile as evp
    monkeypatch.setattr(evp, "_executor", _DummyExecutor())
    maybe_trigger_inference(project, "speaker_a")
    assert submit_calls == []  # not re-fired


def test_maybe_trigger_fires_once_when_pending(tmp_path, monkeypatch):
    project = _bootstrap_project_with_segments(tmp_path)
    create_speaker(project, display_name="C", baseline_speakers=[])
    from services.jobs.editing_voice_profile import maybe_trigger_inference
    submit_calls = []
    class _SyncExecutor:
        def submit(self, fn, *args, **kw):
            submit_calls.append((fn, args, kw))
            with patch(
                "services.jobs.editing_voice_profile.review_pass3_voice_profiles",
                return_value={"speaker_a": {"voice_description": "x"}},
            ):
                fn(*args, **kw)
            f = Future(); f.set_result(None); return f
    from services.jobs import editing_voice_profile as evp
    monkeypatch.setattr(evp, "_executor", _SyncExecutor())
    maybe_trigger_inference(project, "speaker_a")
    assert len(submit_calls) == 1
    sp = next(s for s in load_speakers(project) if s.speaker_id == "speaker_a")
    assert sp.profile_status == "ready"


def test_no_paid_api_imports():
    """Hard guard: the module must NOT import any TTS / clone module."""
    import ast
    src = Path("src/services/jobs/editing_voice_profile.py").read_text("utf-8")
    tree = ast.parse(src)
    forbidden = ("tts_generator", "voice_clone", "minimax_clone", "voice_clone_router")
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mod = (getattr(node, "module", None) or "")
            for alias in getattr(node, "names", []):
                full = f"{mod}.{alias.name}".strip(".")
                for f in forbidden:
                    assert f not in full, f"forbidden import: {full}"
```

- [ ] **Step 2: Implement**

```python
# src/services/jobs/editing_voice_profile.py
"""Fire-and-forget voice profile inference for editing-mode speakers.

Triggered when a freshly-created editing speaker first gets a segment
assigned. Calls Pass 3 of the S2 reviewer (admin-configured multimodal
LLM, mode='studio' → see transcript_reviewer._get_prompt_model). LLM-only,
no TTS / no clone — D26 hard constraint.

Concurrency: a module-level ThreadPoolExecutor (max_workers=2). Tests
inject a dummy executor via monkeypatching ``_executor`` to run sync.
"""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import Any

from services._file_lock import file_lock
from services.jobs.editing_speakers import (
    load_speakers, editing_speakers_path,
)
from services.transcript_reviewer import review_pass3_voice_profiles

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="editvp")


def _update_speaker_status(
    project_dir: str | Path,
    speaker_id: str,
    *,
    status: str,
    error: str | None = None,
    profile: dict | None = None,
) -> None:
    path = editing_speakers_path(project_dir)
    with file_lock(path):
        speakers = load_speakers(project_dir)
        for sp in speakers:
            if sp.speaker_id == speaker_id:
                sp.profile_status = status
                sp.profile_error = error
                if profile is not None:
                    sp.voice_profile = profile
                break
        payload = {"version": 1, "speakers": [asdict(s) for s in speakers]}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")


def _gather_inference_inputs(
    project_dir: Path, speaker_id: str,
) -> tuple[list[dict], Path | None, dict[str, dict]]:
    """Build (lines, source_audio_path, speakers) for review_pass3_voice_profiles.

    - lines: read editing/segments.json; map each segment to a transcript-
      style line {start_ms, end_ms, speaker_id, text}. Need ≥1 with
      speaker_id == target, otherwise Pass 3 returns its fallback.
    - source_audio_path: read manifest.json::audio_source_path. If missing,
      fall back to the first .wav under <project_dir>/audio/.
    - speakers: minimal {speaker_id: {}} for the target.
    """
    seg_path = project_dir / "editor" / "editing" / "segments.json"
    segments_raw = json.loads(seg_path.read_text("utf-8")) if seg_path.is_file() else []
    if isinstance(segments_raw, dict):
        segments_raw = segments_raw.get("segments", [])
    lines = [
        {
            "start_ms": int(s.get("start_ms", 0)),
            "end_ms": int(s.get("end_ms", 0)),
            "speaker_id": s.get("speaker_id"),
            "text": s.get("source_text") or s.get("cn_text") or "",
        }
        for s in segments_raw if isinstance(s, dict)
    ]

    # 主路径：pipeline 落地的 <project_dir>/audio/original.wav
    # （process.py:1401/1463/3323 等多处写入）。
    src_audio: Path | None = None
    primary = project_dir / "audio" / "original.wav"
    if primary.is_file():
        src_audio = primary
    else:
        # fallback：兼容历史目录布局
        audio_dir = project_dir / "audio"
        if audio_dir.is_dir():
            for cand in audio_dir.glob("*.wav"):
                src_audio = cand
                break

    return lines, src_audio, {speaker_id: {}}


def infer_voice_profile_for_speaker(
    project_dir: str | Path, speaker_id: str,
) -> None:
    """Synchronous inference body. Status flow: inferring → ready/failed."""
    project_dir = Path(project_dir)
    _update_speaker_status(project_dir, speaker_id, status="inferring")
    try:
        lines, src_audio, speakers_meta = _gather_inference_inputs(
            project_dir, speaker_id,
        )
        result = review_pass3_voice_profiles(
            lines=lines,
            source_audio_path=src_audio,
            speakers=speakers_meta,
            mode="studio",  # D3
        )
        profile = (result or {}).get(speaker_id) or {}
        _update_speaker_status(
            project_dir, speaker_id, status="ready", profile=profile,
        )
    except Exception as exc:  # fail-soft (D5)
        logger.exception("editing voice profile inference failed for %s", speaker_id)
        _update_speaker_status(
            project_dir, speaker_id, status="failed", error=str(exc)[:200],
        )


def maybe_trigger_inference(project_dir: str | Path, speaker_id: str) -> None:
    """Idempotent: only fires if profile_status == 'pending_segments'.

    Returns immediately; the actual inference runs on the module
    ThreadPoolExecutor. (D4 fire-and-forget.)
    """
    speakers = load_speakers(project_dir)
    target = next((s for s in speakers if s.speaker_id == speaker_id), None)
    if target is None or target.profile_status != "pending_segments":
        return
    _executor.submit(infer_voice_profile_for_speaker, project_dir, speaker_id)
```

- [ ] **Step 3: Wire into PATCH (after speaker_id reassignment commits)**

```python
# src/services/jobs/editing_segments.py — at the end of patch_editing_segment,
# after _atomic_write_json (around line 408):
if speaker_changed:
    try:
        from services.jobs.editing_voice_profile import maybe_trigger_inference
        maybe_trigger_inference(project_dir, applied["speaker_id"])
    except Exception:
        logger.exception("maybe_trigger_inference failed; continuing")
```

- [ ] **Step 4: Add retry endpoint** `POST /editing/speakers/{speaker_id}/retry-profile`

**Job API dispatch (api.py)**：

```python
elif (
    method == "POST"
    and len(path_parts) == 6
    and path_parts[2] == "editing"
    and path_parts[3] == "speakers"
    and path_parts[5] == "retry-profile"
):
    record = self._service.store.get_record(job_id)
    if not record or record.status != "editing":
        self._write_json(404, {"error": "not_in_editing"}); return
    speaker_id = path_parts[4]
    if not re.fullmatch(r"speaker_[a-z0-9_]{1,16}", speaker_id):
        self._write_json(422, {"error": "bad_speaker_id"}); return
    from services.jobs.editing_voice_profile import (
        _update_speaker_status, maybe_trigger_inference,
    )
    _update_speaker_status(record.project_dir, speaker_id, status="pending_segments")
    maybe_trigger_inference(record.project_dir, speaker_id)
    self._write_json(202, {"speaker_id": speaker_id, "status": "pending_segments"})
    return
```

**Gateway dispatch (`_is_post_edit_mutation_subpath` 加分支)**：现有函数底部已有 `parts = subpath.split("/")` + `parts[0] == "segments"` 风格分支，仿照加：

```python
if (
    len(parts) == 4
    and parts[0] == "editing"
    and parts[1] == "speakers"
    and parts[3] == "retry-profile"
):
    return True
```

**Test (在 test_phase1_guards.py)**：
```python
def test_retry_profile_path_in_post_edit_whitelist():
    from gateway.job_intercept import _is_post_edit_mutation_subpath
    assert _is_post_edit_mutation_subpath("editing/speakers/speaker_c/retry-profile")
```

**Idempotency**：retry 把 status 重置成 `pending_segments`，让 `maybe_trigger_inference` 的 idempotent gate 重新放行。已有的 `test_maybe_trigger_idempotent_skips_when_not_pending` 反向验证；同时新增：
```python
def test_retry_resets_status_and_refires(tmp_path, monkeypatch):
    # bootstrap + 把 sp 改成 failed → call retry endpoint logic →
    # assert status was reset to pending_segments before trigger
    ...
```

- [ ] **Step 5: Run + commit**

```bash
python -m pytest tests/test_editing_voice_profile_async.py tests/test_editing_segments_speaker_reassign.py -v
git add src/services/jobs/editing_voice_profile.py \
        src/services/jobs/editing_segments.py \
        tests/test_editing_voice_profile_async.py
git commit -m "feat(editing): async voice profile inference for new speakers"
```

---

### Task 6: Frontend API client + types

**Files:**
- Modify: `frontend-next/src/lib/api/editing.ts`（D14：types 同位；D15：用 `apiClient`，不裸 `fetch`）

- [ ] **Step 1: Add `EditingSpeaker` interface + 3 API functions**

```typescript
// frontend-next/src/lib/api/editing.ts — add near EditingSegment

export interface EditingSpeaker {
  speaker_id: string
  display_name: string
  color?: string | null
  source: "baseline" | "editing"
  profile_status: "pending_segments" | "inferring" | "ready" | "failed"
  profile_error?: string | null
  voice_profile?: Record<string, unknown> | null
}

export async function listEditingSpeakers(
  jobId: string,
): Promise<EditingSpeaker[]> {
  const body = await apiClient.get<{ speakers: EditingSpeaker[] }>(
    `/jobs/${jobId}/editing/speakers`,
  )
  return body.speakers
}

export class DisplayNameConflict extends Error {
  constructor() { super("display_name_conflict") }
}

export async function createEditingSpeaker(
  jobId: string, displayName: string,
): Promise<EditingSpeaker> {
  try {
    return await apiClient.post<EditingSpeaker>(
      `/jobs/${jobId}/editing/speakers`,
      { body: { display_name: displayName } },
    )
  } catch (e: unknown) {
    // apiClient 抛 ApiError，结构见 lib/api/client.ts。grep 'status === 409'
    // 找现有处理（如 voice-clone 409）照搬。
    if ((e as { status?: number }).status === 409) {
      throw new DisplayNameConflict()
    }
    throw e
  }
}

export async function retryEditingSpeakerProfile(
  jobId: string, speakerId: string,
): Promise<void> {
  await apiClient.post<void>(
    `/jobs/${jobId}/editing/speakers/${speakerId}/retry-profile`,
    { body: {} },
  )
}
```

> URL 不带 `/job-api` 前缀（apiClient 内部 `resolveJobApiBaseUrl` 已注入）；POST body 必须包在 `{ body: {...} }` options 里。两点都与 editing.ts 现有 line 169/176/190/202 等 apiClient 调用一致。
>
> 实现前 `Read` 一次 `frontend-next/src/lib/api/client.ts` 确认 `ApiError.status` 字段名（`grep 'class ApiError' frontend-next/src/lib/api/client.ts`）。

- [ ] **Step 2: Commit**

```bash
git add frontend-next/src/lib/api/editing.ts
git commit -m "feat(frontend): editing speakers API client"
```

---

### Task 7: Speaker create dialog component

**Files:**
- Create: `frontend-next/src/components/workspace/EditPageSpeakerCreateDialog.tsx`

- [ ] **Step 1: Component skeleton**

```tsx
// EditPageSpeakerCreateDialog.tsx
"use client";
import { useState } from "react";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import {
  createEditingSpeaker,
  DisplayNameConflict,
  type EditingSpeaker,
} from "@/lib/api/editing";

interface Props {
  jobId: string;
  open: boolean;
  existingNames: Set<string>;
  onClose: () => void;
  onCreated: (sp: EditingSpeaker) => void;
}

export function EditPageSpeakerCreateDialog({
  jobId, open, existingNames, onClose, onCreated,
}: Props) {
  const [name, setName] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const trimmed = name.trim();
  const localConflict = trimmed.length > 0 && existingNames.has(trimmed);

  const handleSubmit = async () => {
    if (!trimmed || localConflict || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const sp = await createEditingSpeaker(jobId, trimmed);
      onCreated(sp);
      setName("");
      onClose();
    } catch (e: unknown) {
      setError(e instanceof DisplayNameConflict
        ? "已存在同名说话人，请改一个名字"
        : "创建失败，请重试");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>新增说话人</DialogTitle>
        </DialogHeader>
        <div className="space-y-3">
          <Input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="例：桑达尔·皮查伊"
            maxLength={40}
            autoFocus
          />
          {localConflict && (
            <p className="text-sm text-red-500">已有同名说话人</p>
          )}
          {error && <p className="text-sm text-red-500">{error}</p>}
          <p className="text-xs text-muted-foreground">
            创建后请到段落下拉里把属于这个说话人的段都改归属。
            后台会自动跑一次音色画像推断（约 5–15 秒）。
          </p>
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={onClose}>取消</Button>
            <Button
              onClick={handleSubmit}
              disabled={!trimmed || localConflict || submitting}
            >
              {submitting ? "创建中..." : "创建"}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
```

- [ ] **Step 2: Commit**

```bash
git add frontend-next/src/components/workspace/EditPageSpeakerCreateDialog.tsx
git commit -m "feat(frontend): EditPageSpeakerCreateDialog component"
```

---

### Task 8: 翻译修改 Tab dropdown 末尾入口 + 音色修改 Tab 顶部按钮 + profile 状态徽章

**Files:**
- Create: `frontend-next/src/components/workspace/EditPageSpeakerProfileBadge.tsx`
- Modify: `frontend-next/src/app/(app)/workspace/[jobId]/edit/page.tsx`

- [ ] **Step 1: Profile badge component**

```tsx
// EditPageSpeakerProfileBadge.tsx
"use client";
import type { EditingSpeaker } from "@/lib/api/editing";
import { Badge } from "@/components/ui/badge";

interface Props { speaker: EditingSpeaker; onRetry?: () => void; }

export function EditPageSpeakerProfileBadge({ speaker, onRetry }: Props) {
  if (speaker.source === "baseline") return null;
  const map = {
    pending_segments: ["待归属段落", "outline"],
    inferring: ["音色画像推断中...", "secondary"],
    ready: ["音色画像就绪", "default"],
    failed: ["推断失败", "destructive"],
  } as const;
  const [text, variant] = map[speaker.profile_status] ?? ["", "outline"];
  return (
    <div className="inline-flex items-center gap-2">
      <Badge variant={variant as never}>{text}</Badge>
      {speaker.profile_status === "failed" && onRetry && (
        <button
          onClick={onRetry}
          className="text-xs text-blue-500 hover:underline"
        >
          重试
        </button>
      )}
    </div>
  );
}
```

- [ ] **Step 2: edit/page.tsx — add `+ 新增说话人...` to translation segment dropdown**

Locate the speaker dropdown rendering (currently scans `segments` for unique speaker_ids). Replace the hard-coded list with `editingSpeakers` state (sourced from `listEditingSpeakers`):

```tsx
// inside the segment row, where the speaker dropdown is rendered:
<select
  value={segmentSpeakers[seg.segment_id] ?? seg.speaker_id}
  onChange={(e) => {
    if (e.target.value === "__create__") {
      setCreateDialogOpen(true);
      return;
    }
    handleSpeakerChange(seg.segment_id, e.target.value);
  }}
>
  {editingSpeakers.map((sp) => (
    <option key={sp.speaker_id} value={sp.speaker_id}>{sp.display_name}</option>
  ))}
  <option value="__create__">+ 新增说话人...</option>
</select>
```

`editingSpeakers` is loaded once on mount + refetched after `onCreated` from the dialog.

- [ ] **Step 3: 音色修改 Tab 顶部按钮**

In the voice modification tab body, add at top:

```tsx
<div className="flex items-center justify-between mb-4">
  <h2 className="text-lg font-semibold">音色修改</h2>
  <Button variant="outline" onClick={() => setCreateDialogOpen(true)}>
    + 新增说话人
  </Button>
</div>
```

- [ ] **Step 4: Render profile badge for each editing-source speaker**

Inside each speaker card in the voice modification tab:

```tsx
<EditPageSpeakerProfileBadge
  speaker={sp}
  onRetry={async () => {
    await retryEditingSpeakerProfile(jobId, sp.speaker_id);
    refetchSpeakers();
  }}
/>
```

- [ ] **Step 5: Frontend feature flag (D29 双端 gate)**

新增 + 修改 入口都包在 `process.env.NEXT_PUBLIC_ENABLE_POST_EDIT === "1"` 下。
现有 `/workspace/[jobId]/edit/page.tsx` 顶部应该已有这个 gate（D29 已落地）；只需确认创建按钮 / dialog 也在该 gate 范围内即可。如顶层已 gate，子节点不用再 gate（dead code 不渲染）。

- [ ] **Step 6: Commit**

```bash
git add frontend-next/src/components/workspace/EditPageSpeakerProfileBadge.tsx \
        frontend-next/src/app/\(app\)/workspace/\[jobId\]/edit/page.tsx
git commit -m "feat(frontend): wire add-speaker into edit page (both tabs)"
```

---

### Task 9: Commit overwrite + copy_as_new — speakers.json 落地

**Files:**
- Modify: `src/services/jobs/editing_commit.py` （`_commit_overwrite` + `_commit_copy_as_new`）
- Test: `tests/test_editing_commit_speakers.py` (new)

**注意（Reviewer P1#2/#3）**：
- baseline schema 真实是 `review_state.json::stages.speaker_review.payload.speaker_names: dict[sid, display_name]` + `speaker_options: list[{speaker_id, display_name}]`（**没有** `.speakers` 字段）。voice_profile 不放 speaker_review，放 `voice_selection_review.payload.voice_profiles[speaker_id]`（D13）
- `_commit_copy_as_new` 完成后新 job 没有 `editor/editing/` 子树（Phase A 已 apply draft + 进 succeeded）。所以必须在 commit 内部、editing/ 还在的时候**读出** editing speakers，传给两阶段 commit 函数，让其落到新 job 的 review_state.json
- 先 read `editing_commit.py` 实际 `_commit_overwrite` / `_commit_copy_as_new` 实现细节，找到 review_state 写入的现成 helper（grep `review_state\|speaker_review`），别另写 _save_review_state

- [ ] **Step 1: 先做调研，把已有 review_state 写入 helper 摸清楚**

```bash
grep -n "speaker_review\|speaker_names\|review_state" src/services/jobs/editing_commit.py src/services/review_state.py | head -30
```

把可复用的 setter（如 `ReviewStateManager.set_stage_payload`）记下来。

- [ ] **Step 2: Failing tests**

```python
# tests/test_editing_commit_speakers.py
import json
from pathlib import Path
import pytest


def test_overwrite_commit_writes_editing_speakers_to_baseline(tmp_path):
    """编辑期加了 speaker_c → overwrite commit 后，baseline review_state
    的 speaker_names 包含 speaker_c → display_name 映射。"""
    # bootstrap: status=editing job + 2 baseline speakers + speaker_c in
    # editing/speakers.json + ≥1 segment whose speaker_id="speaker_c"
    # call _commit_overwrite(...)
    # assert json.loads((project/"review/review_state.json").read_text())
    #     ["stages"]["speaker_review"]["payload"]["speaker_names"]["speaker_c"] == "C"
    ...  # spec; flesh out in implementation


def test_copy_as_new_commit_copies_speakers_to_new_job_review_state(tmp_path):
    """copy_as_new 后新 job 的 review_state 有 speaker_c；源 job 不动。"""
    ...


def test_voice_profile_lands_in_voice_selection_review_payload(tmp_path):
    """voice_profile 数据写到 voice_selection_review.payload.voice_profiles，
    不是 speaker_review.payload."""
    ...
```

- [ ] **Step 3: Implementation sketch**

```python
# src/services/jobs/editing_commit.py — _commit_overwrite, after the
# segments / voice_map / draft files have been merged into baseline:

from services.jobs.editing_speakers import load_speakers
edit_speakers = load_speakers(project_dir)
if edit_speakers:
    _merge_editing_speakers_into_review_state(project_dir, edit_speakers)


def _merge_editing_speakers_into_review_state(
    project_dir: Path, edit_speakers: list,
) -> None:
    """Project speaker_review payload + voice_selection_review payload."""
    rs_path = project_dir / "review" / "review_state.json"
    if not rs_path.is_file():
        return
    rs = json.loads(rs_path.read_text("utf-8"))

    # speaker_review.payload.speaker_names + speaker_options
    sr = rs.setdefault("stages", {}).setdefault("speaker_review", {})
    payload = sr.setdefault("payload", {})
    names = payload.setdefault("speaker_names", {})
    for sp in edit_speakers:
        names[sp.speaker_id] = sp.display_name
    payload["speaker_options"] = [
        {"speaker_id": sid, "display_name": dn}
        for sid, dn in sorted(names.items())
    ]

    # voice_selection_review.payload.voice_profiles[speaker_id] = profile
    vsr = rs["stages"].setdefault("voice_selection_review", {})
    vsr_payload = vsr.setdefault("payload", {})
    profiles = vsr_payload.setdefault("voice_profiles", {})
    for sp in edit_speakers:
        if sp.voice_profile:
            profiles[sp.speaker_id] = sp.voice_profile

    rs_path.write_text(json.dumps(rs, ensure_ascii=False, indent=2), "utf-8")
```

For `_commit_copy_as_new`: in Phase A (before discarding source's `editor/editing/`), read `edit_speakers = load_speakers(source_project_dir)` and pass it through to the new-job state apply step. Apply `_merge_editing_speakers_into_review_state(new_project_dir, edit_speakers)` after the new job's review_state.json is written.

- [ ] **Step 4: Run tests, commit**

```bash
python -m pytest tests/test_editing_commit_speakers.py -v
git add src/services/jobs/editing_commit.py tests/test_editing_commit_speakers.py
git commit -m "feat(editing): commit merges editing speakers + voice_profiles"
```

---

### Task 10: §16 守卫 + 手测 + 部署

- [ ] **Step 1: Add guard tests + feature flag test**

```python
# tests/test_phase1_guards.py — append
def test_editing_speakers_in_simple_mutation_whitelist():
    from gateway.job_intercept import _POST_EDIT_SIMPLE_MUTATION_SUBPATHS
    assert "editing/speakers" in _POST_EDIT_SIMPLE_MUTATION_SUBPATHS


def test_voice_profile_uses_studio_mode():
    import ast
    from pathlib import Path
    src = Path("src/services/jobs/editing_voice_profile.py").read_text("utf-8")
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # match either bare review_pass3_voice_profiles(...) or attr access
        target = node.func
        name = (
            target.id if isinstance(target, ast.Name)
            else target.attr if isinstance(target, ast.Attribute)
            else None
        )
        if name == "review_pass3_voice_profiles":
            mode_kw = next((kw for kw in node.keywords if kw.arg == "mode"), None)
            assert mode_kw is not None
            assert getattr(mode_kw.value, "value", None) == "studio"
            found = True
    assert found, "review_pass3_voice_profiles call not found"


def test_editing_voice_profile_no_paid_tts_import():
    import ast
    from pathlib import Path
    src = Path("src/services/jobs/editing_voice_profile.py").read_text("utf-8")
    tree = ast.parse(src)
    forbidden = ("tts_generator", "voice_clone", "minimax_clone", "voice_clone_router")
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mod = (getattr(node, "module", None) or "")
            for alias in getattr(node, "names", []):
                full = f"{mod}.{alias.name}".strip(".")
                for f in forbidden:
                    assert f not in full, f"forbidden import: {full}"


def test_post_edit_feature_flag_gates_create_speaker_endpoint(monkeypatch):
    """AVT_ENABLE_POST_EDIT=false → POST /editing/speakers 应在 gateway 层
    被 _is_post_edit_mutation_subpath gate 拦截前先 short-circuit。"""
    monkeypatch.setenv("AVT_ENABLE_POST_EDIT", "false")
    # 使用现有 fixture（仿 test_phase1_guards 已有的 feature-flag 测试模式 — grep
    # 'AVT_ENABLE_POST_EDIT' 找参考），断言 POST 返 404 或被 short-circuit。
    # 如本仓没有现成 fixture，本测试可标 @pytest.mark.skip 留待后续接入。
```

- [ ] **Step 2: Audit log 整合（reviewer P2#5）**

`grep -n user_edit_audit src/services/` 找已有 module。Speaker create 应记一条 audit event：
```python
# 在 POST /editing/speakers handler 成功路径末尾：
from services.user_edit_audit import record_action  # 用真实 module 名替换
record_action(
    job_id=job_id, user_id=user_id, action="editing.speaker_create",
    payload={"speaker_id": sp.speaker_id, "display_name": sp.display_name},
)
```
如现有 audit module 已自动覆盖所有 editing endpoint，这步可省略。

- [ ] **Step 3: Run full test suite**

```bash
python -m pytest tests/ -k "editing or speaker or phase1" --tb=short
```

- [ ] **Step 4: Manual E2E smoke (production)**

1. 已 succeeded 的 2-speaker 任务 → 进编辑
2. 翻译修改 Tab，段 #N 下拉末尾点 `+ 新增说话人...`
3. 输入"测试 C"→ 创建（201）
4. 段 #N 下拉出现"测试 C"→ 选它（PATCH 应 200）
5. 切音色修改 Tab → "测试 C" 卡片，徽章"推断中..."
6. 等 10–15 秒刷新 → 徽章变"音色画像就绪"（或 retry button 出现）
7. 选预设音色 → 应用此说话人
8. 回翻译修改 Tab → 段 #N 显示 voice_dirty
9. 一键重新合成 → 段 #N 出 draft
10. Commit overwrite → list_jobs 显示 3 speakers
11. 验：display_name 重名提交 → 前端弹 "已存在同名说话人"
12. 验：cancel 一个加了 speaker 的 editing → speaker 完全消失，baseline 仍 2 个

- [ ] **Step 5: 部署 + push**

```bash
tar -czf /d/Claude/temp/add-speaker.tar.gz \
    src/services/jobs/editing_speakers.py \
    src/services/jobs/editing_voice_profile.py \
    src/services/jobs/editing_segments.py \
    src/services/jobs/editing_commit.py \
    src/services/jobs/api.py \
    gateway/job_intercept.py
# Upload-Via-154.cmd + Deploy-Via-154.cmd + md5 校验 + restart 双容器（gateway + app）
# 前端 Next.js 重构（standalone）+ Caddy 静态资源
git push
```

---

## Risks & Mitigations

| 风险 | 缓解 |
|---|---|
| Voice profile LLM 高延迟（>30s）让用户以为坏了 | 前端徽章 + tab 切换不阻塞；超时 60s 标 failed 让用户重试 |
| 用户连点 PATCH 触发多次 LLM 调用（费用 spike） | `maybe_trigger_inference` idempotent，按 `profile_status != pending_segments` gate |
| editing/speakers.json 与 review_state baseline 双源不一致 | edit page 始终用 `GET /editing/speakers` 合并视图，单一数据源 |
| commit copy_as_new 漏拷 speakers.json | hardlink 路径覆盖整个 editor/editing/ 子树（包括 speakers.json）；guard test 检查 |
| 用户加 speaker 后取消 commit 想要"保留" | 当前不支持，cancel 即丢；前端在编辑期已加多 speaker 时弹"取消会丢失新加的说话人，确认？"二次确认 |
| 新 speaker 第一段太短（<5s）→ Pass 3 LLM 推断质量降级 | infer_voice_profile_for_speaker 不感知时长，照常调；如返 fallback profile，用户在 UI 看到"画像质量低，多归属几段后重试"提示（v2） |
| 27+ speaker 极端场景（hex 后缀） | 现实里几乎不会，已 fallback；color palette 通过 hash 取 8 色循环，足够区分 |
| Daemon 线程在测试中跑过 tmp_path 清理边界 | 已用模块级 `_executor`，测试 monkeypatch 注入同步 dummy executor；生产中 ThreadPoolExecutor 自带 graceful shutdown |

## Out of Scope (本 plan 不做)

- 删除 speaker
- 编辑 speaker display_name（用现有 review_state.speaker_names 在线编辑即可）
- 跨任务 speaker 模板
- speaker 头像 / 自定义颜色 picker
- 字幕 / 剪映草稿对 speaker 的差异化展示

---

## Estimated Effort

- 后端：6 tasks × 0.5–1h = 4–6 h
- 前端：3 tasks × 1–1.5h = 3–4.5 h
- 测试 + 部署 + E2E：2 h
- **合计 ~9–12 h（1–1.5 工作日）**
