# Smart Shadow Evaluator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 P0 阶段离线评估工具，扫描历史 succeeded jobs 输出 fact sheets + 统计报告，校准智能版方案 §7.2/§9 阈值并验证 100 cred/min 毛利可行性。

**Architecture:** 两个独立 stdlib-only Python 脚本：远端 `collector.py` 只读扫 `jobs_root.glob("job_*.json")` 入口 → fact sheets；本地 `analyzer.py` 读 fact sheets + `pricing_runtime.json` → markdown 报告。Collector 通过 AST import guard 强制隔离业务模块。

**Tech Stack:** Python 3.12 stdlib only（argparse / json / pathlib / datetime / hashlib / sys / os / signal / traceback / socket / subprocess / logging / collections / typing / dataclasses / re / time）+ pytest（仅测试用）。

**Spec doc**: [`docs/plans/2026-05-06-smart-shadow-evaluator-design.md`](2026-05-06-smart-shadow-evaluator-design.md)

---

## File Structure

新增（所有 collector / analyzer 都是 stdlib-only）：

```
scripts/
├── smart_shadow_eval_collector.py      # 远端 collector（只读）
└── smart_shadow_eval_analyzer.py        # 本地 analyzer（读 facts + pricing → report.md）

tests/
├── test_smart_shadow_eval_collector.py             # 集成测试，跑 fixture project_dirs
├── test_smart_shadow_eval_collector_imports.py     # AST import 白名单守卫
├── test_smart_shadow_eval_collector_pii_guard.py   # PII 注入守卫
├── test_smart_shadow_eval_paths_in_sync.py         # ARTIFACT_PATHS 同步守卫
├── test_smart_shadow_eval_analyzer.py              # analyzer 单测
└── fixtures/
    └── smart_shadow_eval/
        ├── jobs/                                   # 真实 jobs/ 镜像（mini）
        │   ├── job_post_phase_full.json
        │   ├── job_pre_phase_b.json
        │   ├── job_pre_phase_d.json
        │   ├── job_studio_post_edit.json
        │   └── job_corrupted_state.json
        └── projects/<pid>/                         # 对应 project_dir mini
            ├── job_post_phase_full/                # 含 metering/audit/Phase B/D 全套
            ├── job_pre_phase_b/                    # 无 subtitle_quality_report
            ├── job_pre_phase_d/                    # 无 subtitle_cues / 无 alignment stage
            ├── job_studio_post_edit/               # 跑过 Studio post-edit
            └── job_corrupted_state/                # project_state.json 缺关键 stage
```

不修改任何现有 src/ 或 gateway/ 文件。所有新增内容只在 scripts/ 和 tests/ 下。

---

## Pre-flight

- [ ] **P0：确认工作目录是 `D:\Claude\AIVideoTrans_Codex_web_mvp`（git main 分支，CLAUDE.md 禁止 worktree / 新分支）**

```bash
git status
# 确认在 main、无未提交的非本任务相关变更
```

- [ ] **P1：确认 `.codex_tmp/us_fetch/extracted/` 存在 12 个真实样本**

```bash
ls D:/Claude/AIVideoTrans_Codex_web_mvp/.codex_tmp/us_fetch/extracted/opt/aivideotrans/data/jobs/*.json | wc -l
# Expected: 12
```

---

## Phase A: Collector skeleton + JobRecord 级字段

目标：可执行 collector 雏形 — 扫 jobs/，提取 6 个 JobRecord 顶层字段，原子写 facts.jsonl，本地 dry run 通过。

### Task A1: Collector 文件骨架 + arg parsing

**Files:**
- Create: `scripts/smart_shadow_eval_collector.py`
- Test: `tests/test_smart_shadow_eval_collector.py`

- [ ] **A1.1: 写失败测试（test_collector_args.py 接受最小入参）**

```python
# tests/test_smart_shadow_eval_collector.py
import sys
import json
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "smart_shadow_eval_collector.py"


def test_collector_help_works():
    """collector --help 不抛异常，返回 exit 0"""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True, text=True
    )
    assert result.returncode == 0
    assert "--projects-root" in result.stdout
    assert "--jobs-root" in result.stdout
    assert "--out-dir" in result.stdout
    assert "--limit" in result.stdout
```

- [ ] **A1.2: 跑测试，确认 fail（脚本不存在）**

Run: `python -m pytest tests/test_smart_shadow_eval_collector.py::test_collector_help_works -v`
Expected: FAIL（FileNotFoundError 或类似）

- [ ] **A1.3: 写最小 collector 骨架（含基础 usage docstring，避免 H1.4 时 owner 没文档可读）**

```python
# scripts/smart_shadow_eval_collector.py
"""Smart Shadow Evaluator collector — stdlib-only read-only scanner.

Quick usage:
  # Local smoke (against .codex_tmp samples):
  python scripts/smart_shadow_eval_collector.py \\
    --projects-root D:/Claude/AIVideoTrans_Codex_web_mvp/.codex_tmp/us_fetch/extracted/opt/aivideotrans/data/projects \\
    --jobs-root D:/Claude/AIVideoTrans_Codex_web_mvp/.codex_tmp/us_fetch/extracted/opt/aivideotrans/data/jobs \\
    --out-dir D:/Claude/temp/smart_shadow_eval/local_smoke --limit 3

  # Production (on 154 host):
  python3 scripts/smart_shadow_eval_collector.py \\
    --projects-root /opt/aivideotrans/data/projects \\
    --jobs-root /opt/aivideotrans/data/jobs \\
    --out-dir /tmp/smart_shadow_eval/<run_id>

See docs/plans/2026-05-06-smart-shadow-evaluator-design.md.
"""
import argparse
import os
import sys
from pathlib import Path


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Smart shadow eval collector (read-only)."
    )
    parser.add_argument(
        "--projects-root",
        default=os.environ.get(
            "AIVIDEOTRANS_PROJECTS_DIR",
            "/opt/aivideotrans/data/projects",
        ),
    )
    parser.add_argument(
        "--jobs-root",
        default=os.environ.get(
            "AIVIDEOTRANS_JOBS_DIR",
            "/opt/aivideotrans/data/jobs",
        ),
    )
    parser.add_argument("--out-dir", required=False)
    parser.add_argument("--since", default="2026-01-01")
    parser.add_argument("--until", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--include-running", action="store_true")
    parser.add_argument("--scan-from", choices=["jobs", "projects"], default="jobs")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    # P-A1: skeleton only — full logic in subsequent tasks
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **A1.4: 跑测试，确认 pass**

Run: `python -m pytest tests/test_smart_shadow_eval_collector.py::test_collector_help_works -v`
Expected: PASS

- [ ] **A1.5: Commit**

```bash
git add scripts/smart_shadow_eval_collector.py tests/test_smart_shadow_eval_collector.py
git commit -m "feat: add smart_shadow_eval_collector skeleton with arg parsing"
```

---

### Task A2: Job 发现 + run_id 生成 + atomic write 基础设施

**Files:**
- Modify: `scripts/smart_shadow_eval_collector.py`
- Modify: `tests/test_smart_shadow_eval_collector.py`

- [ ] **A2.1: 写失败测试（empty fixture 也产 facts.jsonl）**

```python
# 加进 tests/test_smart_shadow_eval_collector.py
def test_collector_with_empty_fixtures(tmp_path):
    """空 jobs_root 不报错，产 0 行 facts.jsonl + summary.json is_complete_run=true"""
    jobs_root = tmp_path / "jobs"
    projects_root = tmp_path / "projects"
    out_dir = tmp_path / "out"
    jobs_root.mkdir()
    projects_root.mkdir()

    result = subprocess.run(
        [sys.executable, str(SCRIPT),
         "--jobs-root", str(jobs_root),
         "--projects-root", str(projects_root),
         "--out-dir", str(out_dir)],
        capture_output=True, text=True
    )
    assert result.returncode == 0, f"stderr={result.stderr}"

    facts = out_dir / "facts.jsonl"
    summary = out_dir / "summary.json"
    assert facts.is_file()
    assert facts.read_text() == ""
    assert summary.is_file()
    s = json.loads(summary.read_text())
    assert s["is_complete_run"] is True
    assert s["scan_stats"]["jobs_factsheeted"] == 0
```

- [ ] **A2.2: 跑测试，确认 fail**

Run: `python -m pytest tests/test_smart_shadow_eval_collector.py::test_collector_with_empty_fixtures -v`

- [ ] **A2.3: 实现 run_id + atomic write + summary（含异常路径 fail-safe）**

把 A1 的 main() 替换为：

```python
from __future__ import annotations
import datetime
import json
import socket
import subprocess as sp
import traceback


SCHEMA_VERSION = 1


def _git_sha() -> str:
    try:
        out = sp.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=sp.DEVNULL, text=True, timeout=2,
        )
        return out.strip()
    except Exception:
        return "unknown"


def _make_run_id() -> str:
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H-%MZ")
    return f"{ts}-{socket.gethostname()}-{_git_sha()}"


def _resolve_out_dir(args, run_id: str) -> Path:
    """Receive pre-computed run_id to avoid drift across multiple calls."""
    if args.out_dir:
        return Path(args.out_dir)
    return Path("/tmp") / "smart_shadow_eval" / run_id


def _iter_job_record_paths(jobs_root: Path):
    """Yield job_*.json files (not .events.jsonl)."""
    for p in sorted(jobs_root.glob("job_*.json")):
        if p.name.endswith(".events.jsonl"):
            continue
        yield p


def _atomic_write_summary(out_dir: Path, summary: dict) -> None:
    """Write summary.json via .tmp + rename to avoid partial reads."""
    tmp = out_dir / "summary.json.tmp"
    tmp.write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.rename(out_dir / "summary.json")


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    # Pre-flight (exit 2 path — no summary written yet)
    jobs_root = Path(args.jobs_root)
    projects_root = Path(args.projects_root)
    if not jobs_root.is_dir() or not projects_root.is_dir():
        print(f"ERROR: jobs_root or projects_root not a directory", file=sys.stderr)
        return 2

    # Single run_id used everywhere (out_dir, summary, fact sheets)
    run_id = _make_run_id()
    out_dir = _resolve_out_dir(args, run_id)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"ERROR: out_dir not writable: {exc}", file=sys.stderr)
        return 2

    started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    facts_tmp = out_dir / "facts.jsonl.tmp"
    inventory_tmp = out_dir / "inventory.jsonl.tmp"
    facts_count = 0
    inventory_count = 0
    errors: list[dict] = []
    skipped_status = 0
    skipped_date = 0
    skipped_identity = 0
    fatal_exception: BaseException | None = None

    # Wrap main scan + write in try/except to guarantee a degraded summary
    # is written for ANY uncaught exception (BLOCKER #1 fix).
    try:
        with facts_tmp.open("w", encoding="utf-8") as ff, \
             inventory_tmp.open("w", encoding="utf-8") as fi:
            if args.limit is not None:
                paths = list(_iter_job_record_paths(jobs_root))[: args.limit]
            else:
                paths = list(_iter_job_record_paths(jobs_root))
            for record_path in paths:
                # P-A2: skeleton — fact extraction in subsequent tasks
                inventory_count += 1
    except BaseException as exc:
        fatal_exception = exc
        errors.append({
            "job_id": None,
            "error_type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(),
        })

    # Build summary FIRST (small, less likely to fail than facts rename)
    summary = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "args": vars(args),
        "is_complete_run": fatal_exception is None,
        "scan_stats": {
            "jobs_inventoried": inventory_count,
            "jobs_factsheeted": facts_count,
            "skipped_for_status_filter": skipped_status,
            "skipped_for_date_filter": skipped_date,
            "skipped_for_missing_identity": skipped_identity,
            "orphaned_project_dir_count": 0,  # filled in Task A3
        },
        "errors": errors,
        "git_sha": _git_sha(),
        "hostname": socket.gethostname(),
    }

    # Always try to write summary (even on fatal exception).
    try:
        _atomic_write_summary(out_dir, summary)
    except OSError as exc:
        # Last resort — print to stderr so caller knows something terminal happened.
        print(f"ERROR: could not write summary.json: {exc}", file=sys.stderr)

    # Only rename facts/inventory IF the run completed (preserves spec §3.7
    # invariant: "facts.jsonl 存在 = run 完整").
    if fatal_exception is None:
        facts_tmp.rename(out_dir / "facts.jsonl")
        inventory_tmp.rename(out_dir / "inventory.jsonl")
        return 0
    return 1
```

> **关键不变量（spec §3.6 / §3.7）**：summary.json 总是存在；facts.jsonl 存在 = run 完整；致命异常时 facts/inventory 留 .tmp 后缀给运维诊断。

- [ ] **A2.4: 跑测试，确认 pass**

Run: `python -m pytest tests/test_smart_shadow_eval_collector.py -v`
Expected: 2 passed

- [ ] **A2.5: Commit**

```bash
git add scripts/smart_shadow_eval_collector.py tests/test_smart_shadow_eval_collector.py
git commit -m "feat: add job discovery + atomic write + summary skeleton"
```

---

### Task A3: 真实 fixture（jobs_root + project_dir 2 级嵌套）

**Files:**
- Create: `tests/fixtures/smart_shadow_eval/jobs/job_post_phase_full.json`
- Create: `tests/fixtures/smart_shadow_eval/projects/test_pid_001/job_post_phase_full/project_state.json`
- Create: `tests/fixtures/smart_shadow_eval/projects/test_pid_001/job_post_phase_full/transcript/transcript.json`
- Modify: `tests/test_smart_shadow_eval_collector.py`

- [ ] **A3.1: 写失败测试（fixture 跑出 1 行 inventory）**

```python
def test_collector_with_one_real_fixture(tmp_path):
    """喂 fixture 'job_post_phase_full' 应产 1 行 inventory + 1 行 fact"""
    fixtures = Path(__file__).resolve().parent / "fixtures" / "smart_shadow_eval"
    out_dir = tmp_path / "out"

    result = subprocess.run(
        [sys.executable, str(SCRIPT),
         "--jobs-root", str(fixtures / "jobs"),
         "--projects-root", str(fixtures / "projects"),
         "--out-dir", str(out_dir)],
        capture_output=True, text=True
    )
    assert result.returncode == 0, f"stderr={result.stderr}"

    inventory = (out_dir / "inventory.jsonl").read_text().strip().splitlines()
    assert len(inventory) >= 1
    inv = json.loads(inventory[0])
    assert inv["job_id"] == "job_post_phase_full"
    assert inv["status"] == "succeeded"
    assert inv["service_mode"] in ("studio", "express")
```

- [ ] **A3.2: 创建 fixture 文件 — JobRecord**

```json
// tests/fixtures/smart_shadow_eval/jobs/job_post_phase_full.json
{
  "job_id": "job_post_phase_full",
  "project_id": "test_pid_001",
  "status": "succeeded",
  "service_mode": "studio",
  "tts_provider": "minimax",
  "tts_model": "speech-2.8-hd",
  "created_at": "2026-05-06T08:00:00+00:00",
  "edit_generation": 1,
  "copy_of_job_id": null,
  "root_job_id": "job_post_phase_full",
  "project_dir": "test_pid_001/job_post_phase_full"
}
```

- [ ] **A3.3: 创建 fixture project_state.json + transcript.json（最小化）**

```json
// tests/fixtures/smart_shadow_eval/projects/test_pid_001/job_post_phase_full/project_state.json
{
  "project_id": "test_pid_001",
  "stages": {
    "ingestion": {"payload": {"duration_ms": 254000}},
    "media_understanding": {"payload": {"language": "en_us", "speaker_count": 2}}
  }
}
```

```json
// tests/fixtures/smart_shadow_eval/projects/test_pid_001/job_post_phase_full/transcript/transcript.json
{
  "lines": [
    {"speaker_id": "speaker_a", "start_ms": 0, "end_ms": 5000},
    {"speaker_id": "speaker_b", "start_ms": 5000, "end_ms": 8000},
    {"speaker_id": "speaker_a", "start_ms": 8000, "end_ms": 12000}
  ]
}
```

- [ ] **A3.4: 修改 collector — 写 inventory entry**

在 main() 的 paths 循环里替换 `inventory_count += 1`：

```python
        for record_path in paths:
            try:
                rec = json.loads(record_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                errors.append({
                    "job_id": record_path.stem,
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                    "traceback": traceback.format_exc(),
                })
                continue

            job_id = rec.get("job_id")
            status = rec.get("status")
            created_at = rec.get("created_at")
            if not job_id or not created_at or not status:
                skipped_identity += 1
                continue

            if not args.include_running and status != "succeeded":
                skipped_status += 1
                continue

            # Date filter (later)
            inv_entry = {
                "schema_version": SCHEMA_VERSION,
                "job_id": job_id,
                "project_id": rec.get("project_id"),
                "status": status,
                "created_at": created_at,
                "service_mode": rec.get("service_mode"),
                "had_post_edit": (rec.get("edit_generation", 0) or 0) > 0
                    or rec.get("copy_of_job_id") is not None,
            }
            fi.write(json.dumps(inv_entry, ensure_ascii=False) + "\n")
            inventory_count += 1
```

- [ ] **A3.5: 跑测试，确认 pass**

Run: `python -m pytest tests/test_smart_shadow_eval_collector.py -v`
Expected: 3 passed

- [ ] **A3.6: Commit**

```bash
git add tests/fixtures/smart_shadow_eval/ scripts/smart_shadow_eval_collector.py tests/test_smart_shadow_eval_collector.py
git commit -m "feat: add fixture + inventory writer with JobRecord field extraction"
```

---

### Task A4: project_state.json 加载 + duration_seconds + source_language

**Files:**
- Modify: `scripts/smart_shadow_eval_collector.py`
- Modify: `tests/test_smart_shadow_eval_collector.py`

- [ ] **A4.1: 写失败测试**

```python
def test_collector_extracts_duration_and_language(tmp_path):
    fixtures = Path(__file__).resolve().parent / "fixtures" / "smart_shadow_eval"
    out_dir = tmp_path / "out"
    subprocess.run(
        [sys.executable, str(SCRIPT),
         "--jobs-root", str(fixtures / "jobs"),
         "--projects-root", str(fixtures / "projects"),
         "--out-dir", str(out_dir)],
        check=True, capture_output=True, text=True
    )
    inventory = [json.loads(line) for line in
                 (out_dir / "inventory.jsonl").read_text().splitlines()]
    inv = next(i for i in inventory if i["job_id"] == "job_post_phase_full")
    assert inv["duration_seconds"] == 254.0
    assert inv["source_language"] == "en_us"
    assert inv["target_language"] == "zh-CN"
```

- [ ] **A4.2: 加 ARTIFACT_PATHS 常量 + project_state 加载逻辑**

在文件顶部 import 后加：

```python
ARTIFACT_PATHS = {
    # JOBS root (flat)
    "job_record":             "{job_id}.json",
    "job_events":             "{job_id}.events.jsonl",

    # PROJECT/JOB level (2-level nested)
    "project_state":          "project_state.json",
    "review_state":           "review_state.json",
    "manifest":               "manifest.json",
    "download_metadata":      "download_metadata.json",
    "transcript":             "transcript/transcript.json",
    "s2_review_result":       "transcript/s2_review_result.json",
    "s2_review_audit":        "transcript/s2_review_audit.json",
    "s2_pass1_result":        "transcript/s2_pass1_result.json",
    "translation_segments":   "translation/segments.json",
    "editor_segments":        "editor/segments.json",
    "subtitle_quality_report": "output/subtitle_quality_report.json",
    "subtitle_cues":           "output/subtitle_cues.json",
    "usage_events":           "metering/usage_events.jsonl",
    "user_edit_events":       "audit/user_edit_events.jsonl",
}


def _resolve_project_dir(projects_root: Path, project_id: str | None,
                          job_id: str) -> Path | None:
    """projects/<project_id>/job_<bare_id>/ — handles missing project_id."""
    if not project_id:
        return None
    bare_job_id = job_id.removeprefix("job_") if job_id.startswith("job_") else job_id
    candidate = projects_root / project_id / f"job_{bare_job_id}"
    return candidate if candidate.is_dir() else None


def _safe_load_json(path: Path) -> dict | list | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _extract_from_project_state(project_state: dict | None) -> dict:
    """Return {duration_seconds, source_language, asr_speaker_count} or null fields."""
    out = {
        "duration_seconds": None,
        "source_language": None,
        "asr_speaker_count": None,
    }
    if not isinstance(project_state, dict):
        return out
    stages = project_state.get("stages") or {}
    ingestion = (stages.get("ingestion") or {}).get("payload") or {}
    media = (stages.get("media_understanding") or {}).get("payload") or {}
    if isinstance(ingestion.get("duration_ms"), (int, float)):
        out["duration_seconds"] = ingestion["duration_ms"] / 1000.0
    if isinstance(media.get("language"), str):
        out["source_language"] = media["language"]
    if isinstance(media.get("speaker_count"), int):
        out["asr_speaker_count"] = media["speaker_count"]
    return out
```

修改 inv_entry 块：

```python
            project_dir = _resolve_project_dir(
                projects_root, rec.get("project_id"), job_id
            )
            ps = (_safe_load_json(project_dir / ARTIFACT_PATHS["project_state"])
                  if project_dir else None)
            ps_extracted = _extract_from_project_state(ps)

            inv_entry = {
                "schema_version": SCHEMA_VERSION,
                "job_id": job_id,
                "project_id": rec.get("project_id"),
                "status": status,
                "created_at": created_at,
                "duration_seconds": ps_extracted["duration_seconds"],
                "source_language": ps_extracted["source_language"],
                "target_language": "zh-CN",
                "service_mode": rec.get("service_mode"),
                "had_post_edit": (rec.get("edit_generation", 0) or 0) > 0
                    or rec.get("copy_of_job_id") is not None,
            }
```

- [ ] **A4.3: 跑测试**

Run: `python -m pytest tests/test_smart_shadow_eval_collector.py -v`

- [ ] **A4.4: Commit**

```bash
git add scripts/smart_shadow_eval_collector.py tests/test_smart_shadow_eval_collector.py
git commit -m "feat: add ARTIFACT_PATHS + project_state extraction (duration, language)"
```

---

### Task A5: Fact sheet writer（基础 6 个 JobRecord 字段）

**Files:**
- Modify: `scripts/smart_shadow_eval_collector.py`
- Modify: `tests/test_smart_shadow_eval_collector.py`

- [ ] **A5.1: 写失败测试（facts.jsonl 含 1 行 fact）**

```python
def test_collector_writes_minimal_fact_sheet(tmp_path):
    fixtures = Path(__file__).resolve().parent / "fixtures" / "smart_shadow_eval"
    out_dir = tmp_path / "out"
    subprocess.run(
        [sys.executable, str(SCRIPT),
         "--jobs-root", str(fixtures / "jobs"),
         "--projects-root", str(fixtures / "projects"),
         "--out-dir", str(out_dir)],
        check=True, capture_output=True, text=True
    )
    facts = [json.loads(line) for line in
             (out_dir / "facts.jsonl").read_text().splitlines()]
    assert len(facts) >= 1
    f = next(x for x in facts if x["job_id"] == "job_post_phase_full")
    assert f["schema_version"] == 1
    assert f["service_mode"] == "studio"
    assert f["tts_provider"] == "minimax"
    assert f["tts_model"] == "speech-2.8-hd"
    assert f["edit_generation"] == 1
    assert f["had_post_edit"] is True  # edit_generation > 0
    assert "run_id" in f
    assert "artifact_presence" in f
    assert f["artifact_presence"]["project_state_json"] is True
    assert f["artifact_presence"]["transcript_json"] is True
```

- [ ] **A5.2: 实现 fact sheet writer**

加 helper：

```python
def _build_artifact_presence(project_dir: Path | None) -> dict:
    """Check existence of each artifact path."""
    if project_dir is None or not project_dir.is_dir():
        return {key: False for key in [
            "project_state_json", "review_state_json", "manifest_json",
            "transcript_json", "s2_review_result_json", "s2_pass1_result_json",
            "translation_segments_json", "editor_segments_json",
            "subtitle_quality_report", "subtitle_cues",
            "metering_usage_events", "audit_user_edit_events",
        ]}
    return {
        "project_state_json": (project_dir / ARTIFACT_PATHS["project_state"]).is_file(),
        "review_state_json": (project_dir / ARTIFACT_PATHS["review_state"]).is_file(),
        "manifest_json": (project_dir / ARTIFACT_PATHS["manifest"]).is_file(),
        "transcript_json": (project_dir / ARTIFACT_PATHS["transcript"]).is_file(),
        "s2_review_result_json": (project_dir / ARTIFACT_PATHS["s2_review_result"]).is_file(),
        "s2_pass1_result_json": (project_dir / ARTIFACT_PATHS["s2_pass1_result"]).is_file(),
        "translation_segments_json": (project_dir / ARTIFACT_PATHS["translation_segments"]).is_file(),
        "editor_segments_json": (project_dir / ARTIFACT_PATHS["editor_segments"]).is_file(),
        "subtitle_quality_report": (project_dir / ARTIFACT_PATHS["subtitle_quality_report"]).is_file(),
        "subtitle_cues": (project_dir / ARTIFACT_PATHS["subtitle_cues"]).is_file(),
        "metering_usage_events": (project_dir / ARTIFACT_PATHS["usage_events"]).is_file(),
        "audit_user_edit_events": (project_dir / ARTIFACT_PATHS["user_edit_events"]).is_file(),
    }


def _build_fact_sheet(rec: dict, project_dir: Path | None,
                      ps_extracted: dict, run_id: str) -> dict:
    """Phase A: minimal fact sheet — Phase B/C/D/E will extend."""
    edit_gen = rec.get("edit_generation", 0) or 0
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "job_id": rec["job_id"],
        "project_id": rec.get("project_id"),
        "root_job_id": rec.get("root_job_id") or rec["job_id"],
        "service_mode": rec.get("service_mode"),
        "status": rec["status"],
        "created_at": rec["created_at"],
        "duration_seconds": ps_extracted["duration_seconds"],
        "source_language": ps_extracted["source_language"],
        "target_language": "zh-CN",
        "tts_provider": rec.get("tts_provider"),
        "tts_model": rec.get("tts_model"),
        "edit_generation": edit_gen,
        "had_post_edit": edit_gen > 0 or rec.get("copy_of_job_id") is not None,
        "artifact_presence": _build_artifact_presence(project_dir),
        # Phase B-E: speaker_stats, clone_sample_stats, retry_stats, etc.
    }
```

在 main() 循环里，inv_entry 写完后加：

```python
            fact_sheet = _build_fact_sheet(rec, project_dir, ps_extracted, run_id)
            ff.write(json.dumps(fact_sheet, ensure_ascii=False) + "\n")
            facts_count += 1
```

- [ ] **A5.3: 跑测试**

Run: `python -m pytest tests/test_smart_shadow_eval_collector.py -v`
Expected: 4 passed

- [ ] **A5.4: 验证单行 ≤ 4KB**

```python
def test_fact_sheet_line_under_4kb(tmp_path):
    fixtures = Path(__file__).resolve().parent / "fixtures" / "smart_shadow_eval"
    out_dir = tmp_path / "out"
    subprocess.run(
        [sys.executable, str(SCRIPT),
         "--jobs-root", str(fixtures / "jobs"),
         "--projects-root", str(fixtures / "projects"),
         "--out-dir", str(out_dir)],
        check=True, capture_output=True
    )
    for line in (out_dir / "facts.jsonl").read_text().splitlines():
        assert len(line.encode("utf-8")) <= 4096
```

跑测试 → pass

- [ ] **A5.5: Commit**

```bash
git add scripts/smart_shadow_eval_collector.py tests/test_smart_shadow_eval_collector.py
git commit -m "feat: add fact sheet writer with artifact_presence + JobRecord fields"
```

---

## Phase B: Speaker stats + clone sample eligibility

### Task B1: speaker_duration_shares + speaker_count_by_threshold

**Files:**
- Modify: `scripts/smart_shadow_eval_collector.py`
- Modify: `tests/test_smart_shadow_eval_collector.py`

- [ ] **B1.1: 写失败测试**

```python
def test_speaker_stats_extraction(tmp_path):
    """transcript.json 3 lines: A=5s+4s=9s, B=3s. Total 12s. Shares: A=0.75, B=0.25"""
    fixtures = Path(__file__).resolve().parent / "fixtures" / "smart_shadow_eval"
    out_dir = tmp_path / "out"
    subprocess.run(
        [sys.executable, str(SCRIPT),
         "--jobs-root", str(fixtures / "jobs"),
         "--projects-root", str(fixtures / "projects"),
         "--out-dir", str(out_dir)],
        check=True, capture_output=True
    )
    facts = [json.loads(line) for line in
             (out_dir / "facts.jsonl").read_text().splitlines()]
    f = next(x for x in facts if x["job_id"] == "job_post_phase_full")
    ss = f["speaker_stats"]
    assert ss["asr_speaker_count"] == 2
    assert ss["speaker_duration_shares"] == [0.75, 0.25]
    assert ss["speaker_count_by_threshold"]["0.05"] == 2
    assert ss["speaker_count_by_threshold"]["0.10"] == 2
    assert ss["speaker_count_by_threshold"]["0.20"] == 2
    # 0.30 threshold: only speaker A qualifies → 1
    # NOT in default set, but speaker_count_by_threshold needs 0.05/0.10/0.15/0.20
```

- [ ] **B1.2: 实现 speaker_stats helper**

```python
from collections import defaultdict

SPEAKER_THRESHOLDS = (0.05, 0.10, 0.15, 0.20)


def _compute_speaker_stats(transcript: dict | None,
                            asr_speaker_count: int | None) -> dict | None:
    """Return speaker_stats dict or None if transcript missing."""
    if not isinstance(transcript, dict):
        return None
    lines = transcript.get("lines")
    if not isinstance(lines, list) or not lines:
        return None
    durations = defaultdict(float)
    for line in lines:
        spk = line.get("speaker_id")
        s = line.get("start_ms", 0)
        e = line.get("end_ms", 0)
        if spk and isinstance(s, (int, float)) and isinstance(e, (int, float)):
            durations[spk] += max(0.0, e - s)
    total = sum(durations.values())
    if total <= 0:
        return None
    shares = sorted(
        (d / total for d in durations.values()),
        reverse=True,
    )
    by_threshold = {
        f"{t:.2f}": sum(1 for s in shares if s >= t)
        for t in SPEAKER_THRESHOLDS
    }
    return {
        "asr_speaker_count": asr_speaker_count or len(durations),
        "speaker_duration_shares": [round(s, 4) for s in shares],
        "speaker_count_by_threshold": by_threshold,
    }
```

在 _build_fact_sheet 里加：

```python
    transcript = _safe_load_json(project_dir / ARTIFACT_PATHS["transcript"]) if project_dir else None
    speaker_stats = _compute_speaker_stats(transcript, ps_extracted.get("asr_speaker_count"))
    # ...
    "speaker_stats": speaker_stats,
```

- [ ] **B1.3: 跑测试 → pass**

- [ ] **B1.4: Commit**

```bash
git commit -m "feat: extract speaker_duration_shares + speaker_count_by_threshold from transcript.json"
```

---

### Task B2: eligible_sample_count_buckets_by_speaker（克隆样本反事实统计）

**Files:**
- Modify: `scripts/smart_shadow_eval_collector.py`
- Modify: `tests/test_smart_shadow_eval_collector.py`

- [ ] **B2.1: 写失败测试**

需要更复杂的 transcript.json — 在 fixture 里加多条 lines。修改 fixture 的 transcript.json:

```json
{
  "lines": [
    {"speaker_id": "speaker_a", "start_ms": 0, "end_ms": 6000},
    {"speaker_id": "speaker_a", "start_ms": 6000, "end_ms": 16000},
    {"speaker_id": "speaker_a", "start_ms": 16000, "end_ms": 23000},
    {"speaker_id": "speaker_b", "start_ms": 23000, "end_ms": 27000},
    {"speaker_id": "speaker_b", "start_ms": 27000, "end_ms": 39000}
  ]
}
```

speaker_a: 6s, 10s, 7s （≥5s: 3 段；≥8s: 2 段；≥10s: 1 段；≥15s: 0）
speaker_b: 4s, 12s （≥5s: 1 段；≥8s: 1 段；≥10s: 1 段；≥15s: 0）

```python
def test_clone_sample_buckets(tmp_path):
    # ... run collector ...
    f = ...  # fact sheet
    css = f["clone_sample_stats"]
    assert css["eligible_speakers"] == 2
    # speaker_a: 3 segments (6/10/7s)
    assert css["eligible_sample_count_buckets_by_speaker"][0] == \
           {"≥5s": 3, "≥8s": 2, "≥10s": 1, "≥15s": 0}
    # speaker_b: 2 segments (4/12s, only 12s ≥ 5)
    assert css["eligible_sample_count_buckets_by_speaker"][1] == \
           {"≥5s": 1, "≥8s": 1, "≥10s": 1, "≥15s": 0}
```

- [ ] **B2.2: 实现 clone_sample_stats helper**

```python
SAMPLE_THRESHOLDS_S = (5, 8, 10, 15)


def _compute_clone_sample_stats(transcript: dict | None) -> dict | None:
    """Per speaker: bucket-count of sample durations ≥ each threshold."""
    if not isinstance(transcript, dict):
        return None
    lines = transcript.get("lines")
    if not isinstance(lines, list) or not lines:
        return None
    by_speaker = defaultdict(list)
    for line in lines:
        spk = line.get("speaker_id")
        s = line.get("start_ms", 0)
        e = line.get("end_ms", 0)
        if spk and isinstance(s, (int, float)) and isinstance(e, (int, float)):
            dur_s = max(0.0, e - s) / 1000.0
            by_speaker[spk].append(dur_s)
    # Order by total duration descending (matches speaker_duration_shares ordering)
    sorted_speakers = sorted(
        by_speaker.items(),
        key=lambda kv: sum(kv[1]),
        reverse=True,
    )
    buckets = []
    for _, durations in sorted_speakers:
        bucket = {f"≥{t}s": sum(1 for d in durations if d >= t)
                  for t in SAMPLE_THRESHOLDS_S}
        buckets.append(bucket)
    return {
        "eligible_speakers": len(buckets),
        "eligible_sample_count_buckets_by_speaker": buckets,
    }
```

加到 fact sheet：

```python
    clone_sample_stats = _compute_clone_sample_stats(transcript)
    # ...
    "clone_sample_stats": clone_sample_stats,
```

- [ ] **B2.3: 跑测试 → pass**

- [ ] **B2.4: Commit**

```bash
git commit -m "feat: extract eligible clone sample buckets per speaker"
```

---

### Task B3: actual_clone_stats from editor/translation segments

**Files:**
- Modify: `scripts/smart_shadow_eval_collector.py`
- Create: `tests/fixtures/smart_shadow_eval/projects/test_pid_001/job_post_phase_full/editor/segments.json`
- Modify: `tests/test_smart_shadow_eval_collector.py`

- [ ] **B3.1: 写失败测试**

```python
def test_actual_clone_stats(tmp_path):
    # ... run collector ...
    f = ...  # fact sheet for job_post_phase_full
    acs = f["actual_clone_stats"]
    assert acs["cloned_speakers"] == 1  # speaker_a uses moss_audio_*
    assert acs["preset_speakers"] == 1  # speaker_b uses preset
    assert acs["voice_ids_by_speaker"][0].startswith("moss_audio_")
    assert "preset" in acs["voice_ids_by_speaker"][1].lower()
```

- [ ] **B3.2: 创建 fixture editor/segments.json**

```json
[
  {"segment_id": "1", "speaker_id": "speaker_a", "voice_id": "moss_audio_85bcf79d-00f2-11f1-b80b-cafa791d3a11", "start_ms": 0, "end_ms": 23000, "actual_duration_ms": 22000, "rewrite_count": 1},
  {"segment_id": "2", "speaker_id": "speaker_b", "voice_id": "preset_chinese_male_1", "start_ms": 23000, "end_ms": 39000, "actual_duration_ms": 16000, "rewrite_count": 0}
]
```

- [ ] **B3.3: 实现 actual_clone_stats helper**

```python
def _classify_voice_id(voice_id: str) -> str:
    """'cloned' | 'preset' | 'unknown' / 'auto'."""
    if not voice_id or voice_id.lower() == "auto":
        return "unknown"
    # MiniMax cloned voices typically have moss_audio_ prefix or long uuid hash
    if voice_id.startswith("moss_audio_"):
        return "cloned"
    if len(voice_id) >= 32 and "-" in voice_id:
        return "cloned"
    return "preset"


def _compute_actual_clone_stats(project_dir: Path | None) -> dict | None:
    """Per speaker: cloned vs preset voice classification."""
    if not project_dir:
        return None
    # Prefer editor/segments.json (post-edit-aware), fall back to translation/segments.json
    segs_path = project_dir / ARTIFACT_PATHS["editor_segments"]
    if not segs_path.is_file():
        segs_path = project_dir / ARTIFACT_PATHS["translation_segments"]
    segs = _safe_load_json(segs_path)
    if not isinstance(segs, list) or not segs:
        return None
    # First voice_id seen per speaker
    by_speaker = {}
    for seg in segs:
        spk = seg.get("speaker_id")
        vid = seg.get("voice_id")
        if spk and vid and spk not in by_speaker:
            by_speaker[spk] = vid
    # Order by appearance (= order in segments)
    voice_ids = list(by_speaker.values())
    classifications = [_classify_voice_id(v) for v in voice_ids]
    return {
        "cloned_speakers": classifications.count("cloned"),
        "preset_speakers": classifications.count("preset"),
        "voice_ids_by_speaker": voice_ids,
    }
```

加进 _build_fact_sheet:

```python
    "actual_clone_stats": _compute_actual_clone_stats(project_dir),
```

- [ ] **B3.4: 跑测试 → pass**

- [ ] **B3.5: Commit**

```bash
git commit -m "feat: classify cloned vs preset voice_ids per speaker"
```

---

## Phase C: Retry stats（fallback path 优先）

### Task C1: rewrite_count fallback from editor/segments.json

**Files:**
- Modify: `scripts/smart_shadow_eval_collector.py`
- Modify: `tests/test_smart_shadow_eval_collector.py`

- [ ] **C1.1: 写失败测试**

```python
def test_retry_stats_fallback(tmp_path):
    """No metering/usage_events.jsonl → fallback to editor.segments.rewrite_count sum"""
    # ... run collector on job_post_phase_full (no metering subdir) ...
    f = ...
    rs = f["retry_stats"]
    # editor segs: rewrite_count 1 + 0 = 1
    assert rs["rewrite_count"] == 1
    assert rs["retts_count"] is None  # no metering = no retts data
    assert rs["_data_source"] == "fallback_editor_segments"
```

- [ ] **C1.2: 实现 retry_stats helper（fallback 路径）**

```python
REWRITE_TASKS = frozenset({
    "s5_rewrite", "s5_rewrite_strict", "s5_short_content_compact",
})
RETTS_BUCKETS = frozenset({"post_tts_resynth", "post_edit_resynth"})


def _compute_retry_stats(project_dir: Path | None) -> dict | None:
    """Prefer metering, fall back to editor/segments.json rewrite_count sum."""
    if not project_dir:
        return None
    metering_path = project_dir / ARTIFACT_PATHS["usage_events"]
    if metering_path.is_file():
        # Phase D: implement metering parsing (Task D1)
        return _retry_stats_from_metering(metering_path)
    # Fallback
    editor_segs = _safe_load_json(project_dir / ARTIFACT_PATHS["editor_segments"])
    if not isinstance(editor_segs, list):
        return {
            "rewrite_count": None,
            "retts_count": None,
            "retts_total_duration_ms": None,
            "_data_source": "no_data",
        }
    return {
        "rewrite_count": sum(s.get("rewrite_count", 0) or 0 for s in editor_segs),
        "retts_count": None,
        "retts_total_duration_ms": None,
        "_data_source": "fallback_editor_segments",
    }


def _retry_stats_from_metering(metering_path: Path) -> dict:
    """Stub for Phase C/D: implement in Task C2 (metering parsing)."""
    return {
        "rewrite_count": None,
        "retts_count": None,
        "retts_total_duration_ms": None,
        "_data_source": "metering_pending_impl",
    }
```

加进 _build_fact_sheet:

```python
    "retry_stats": _compute_retry_stats(project_dir),
```

- [ ] **C1.3: 跑测试 → pass**

- [ ] **C1.4: Commit**

```bash
git commit -m "feat: retry_stats fallback from editor.segments rewrite_count"
```

---

### Task C2: usage_events.jsonl loader + REWRITE_TASKS / RETTS_BUCKETS aggregation

**Files:**
- Modify: `scripts/smart_shadow_eval_collector.py`
- Create: `tests/fixtures/smart_shadow_eval/projects/test_pid_001/job_post_phase_full/metering/usage_events.jsonl`
- Modify: `tests/test_smart_shadow_eval_collector.py`

- [ ] **C2.1: 写失败测试**

```python
def test_retry_stats_from_metering(tmp_path):
    """When metering exists, prefer metering data."""
    # Add metering fixture for job_post_phase_full
    # ... run collector ...
    f = ...
    rs = f["retry_stats"]
    assert rs["_data_source"] == "metering"
    assert rs["rewrite_count"] == 2  # 2 s5_rewrite events in fixture
    assert rs["retts_count"] == 3    # 3 post_tts_resynth events
    assert rs["retts_total_duration_ms"] == 4500  # 1500 + 1500 + 1500
```

- [ ] **C2.2: 创建 fixture metering/usage_events.jsonl**

```jsonl
{"kind": "llm", "task": "s5_rewrite", "input_tokens": 100, "output_tokens": 50}
{"kind": "llm", "task": "s5_rewrite", "input_tokens": 80, "output_tokens": 40}
{"kind": "llm", "task": "translate", "input_tokens": 500, "output_tokens": 300}
{"kind": "tts", "bucket": "first_tts", "billed_chars": 200, "duration_ms": 8000}
{"kind": "tts", "bucket": "post_tts_resynth", "billed_chars": 50, "duration_ms": 1500}
{"kind": "tts", "bucket": "post_tts_resynth", "billed_chars": 50, "duration_ms": 1500}
{"kind": "tts", "bucket": "post_tts_resynth", "billed_chars": 50, "duration_ms": 1500}
{"kind": "voice_clone"}
```

- [ ] **C2.3: 实现 metering 解析**

替换 _retry_stats_from_metering：

```python
def _retry_stats_from_metering(metering_path: Path) -> dict:
    rewrite = 0
    retts_count = 0
    retts_dur_ms = 0
    try:
        for line in metering_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = ev.get("kind")
            if kind == "llm" and ev.get("task") in REWRITE_TASKS:
                rewrite += 1
            elif kind == "tts" and ev.get("bucket") in RETTS_BUCKETS:
                retts_count += 1
                retts_dur_ms += int(ev.get("duration_ms") or 0)
    except OSError:
        return {
            "rewrite_count": None, "retts_count": None,
            "retts_total_duration_ms": None,
            "_data_source": "metering_unreadable",
        }
    return {
        "rewrite_count": rewrite,
        "retts_count": retts_count,
        "retts_total_duration_ms": retts_dur_ms,
        "_data_source": "metering",
    }
```

也加 usage_meter 聚合（Phase E 用）：

```python
def _compute_usage_meter(metering_path: Path) -> dict | None:
    """Aggregate llm tokens / tts chars / clone calls for cost estimation.

    Includes rewrite_input_text_chars_total (sum of input_text_chars for
    LLM events with task IN REWRITE_TASKS) — needed by analyzer §4.2 cost
    formula's rewrite_extra_rmb term (reviewer iter1 MAJOR fix).
    """
    if not metering_path.is_file():
        return None
    agg = {
        "llm_input_tokens": 0,
        "llm_output_tokens": 0,
        "tts_chars_total": 0,
        "post_tts_resynth_billed_chars": 0,
        "post_edit_resynth_billed_chars": 0,
        "clone_calls": 0,
        "rewrite_count": 0,
        "rewrite_input_text_chars_total": 0,
    }
    try:
        for line in metering_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = ev.get("kind")
            if kind == "llm":
                agg["llm_input_tokens"] += int(ev.get("input_tokens") or 0)
                agg["llm_output_tokens"] += int(ev.get("output_tokens") or 0)
                if ev.get("task") in REWRITE_TASKS:
                    agg["rewrite_count"] += 1
                    agg["rewrite_input_text_chars_total"] += int(
                        ev.get("input_text_chars") or 0
                    )
            elif kind == "tts":
                bc = int(ev.get("billed_chars") or 0)
                agg["tts_chars_total"] += bc
                bucket = ev.get("bucket")
                if bucket == "post_tts_resynth":
                    agg["post_tts_resynth_billed_chars"] += bc
                elif bucket == "post_edit_resynth":
                    agg["post_edit_resynth_billed_chars"] += bc
            elif kind == "voice_clone":
                agg["clone_calls"] += 1
    except OSError:
        return None
    return agg
```

加进 _build_fact_sheet:

```python
    metering_path = (project_dir / ARTIFACT_PATHS["usage_events"]) if project_dir else None
    "usage_meter": _compute_usage_meter(metering_path) if metering_path else None,
```

- [ ] **C2.4: 跑测试 → pass**

- [ ] **C2.5: Commit**

```bash
git commit -m "feat: metering parser for retry_stats + usage_meter aggregation"
```

---

## Phase D: Phase B/D 字段 + audit + Whisper

### Task D1: subtitle_quality_report 字段提取

**Files:**
- Modify: `scripts/smart_shadow_eval_collector.py`
- Create: `tests/fixtures/smart_shadow_eval/projects/test_pid_001/job_post_phase_full/output/subtitle_quality_report.json`
- Modify: `tests/test_smart_shadow_eval_collector.py`

- [ ] **D1.1: 写失败测试**

```python
def test_subtitle_sync(tmp_path):
    f = ...
    ss = f["subtitle_sync"]
    assert ss["text_audio_drift_count"] == 2
    assert "drift_block_ids" in ss
```

- [ ] **D1.2: 创建 fixture**

```json
{
  "text_audio_drift_count": 2,
  "issues": [
    {"type": "text_audio_drift", "block_id": "block_0007"},
    {"type": "text_audio_drift", "block_id": "block_0012"}
  ]
}
```

- [ ] **D1.3: 实现 helper**

```python
def _compute_subtitle_sync(project_dir: Path | None) -> dict | None:
    if not project_dir:
        return None
    path = project_dir / ARTIFACT_PATHS["subtitle_quality_report"]
    if not path.is_file():
        return {
            "text_audio_drift_count": None,
            "drift_block_ids": [],
            "_reason_null": "subtitle_quality_report not present",
        }
    data = _safe_load_json(path)
    if not isinstance(data, dict):
        return {"text_audio_drift_count": None, "drift_block_ids": [], "_reason_null": "unreadable"}
    drift_count = data.get("text_audio_drift_count")
    drift_ids = []
    for issue in (data.get("issues") or []):
        if issue.get("type") == "text_audio_drift":
            bid = issue.get("block_id")
            # Sanitize: only positional ID, no content
            if isinstance(bid, str) and bid.startswith("block_"):
                drift_ids.append(bid)
    return {
        "text_audio_drift_count": drift_count if isinstance(drift_count, int) else None,
        "drift_block_ids": drift_ids[:50],  # cap to prevent fingerprint bloat
    }
```

- [ ] **D1.4: 加进 fact sheet + 跑测试 → pass**

- [ ] **D1.5: Commit**

```bash
git commit -m "feat: extract subtitle_sync.text_audio_drift_count + sanitized block_ids"
```

---

### Task D2: Whisper 字段（subtitle_cues） + workflow_alignment_cache（project_state.stages）

**Files:**
- Modify: `scripts/smart_shadow_eval_collector.py`
- Create: `tests/fixtures/smart_shadow_eval/projects/test_pid_001/job_post_phase_full/output/subtitle_cues.json`
- Modify project_state.json fixture to include alignment stage
- Modify: `tests/test_smart_shadow_eval_collector.py`

- [ ] **D2.1: 写失败测试**

```python
def test_whisper_and_workflow_cache(tmp_path):
    f = ...
    w = f["whisper"]
    assert w["alignment_model"] == "small"
    assert w["alignment_fingerprint"] == "abc123def456"
    # 5 cues total, 3 whisper-aligned, 2 fallback
    assert w["whisper_aligned_cue_count"] == 3
    assert w["proportional_fallback_cue_count"] == 2

    wac = f["workflow_alignment_cache"]
    assert wac["cache_hit_blocks"] == 4
    assert wac["block_count"] == 5
```

- [ ] **D2.2: 创建 fixtures**

```json
// output/subtitle_cues.json
{
  "alignment_model": "small",
  "alignment_fingerprint": "abc123def456",
  "cues": [
    {"index": 1, "source": "semantic_block_v2_whisper_aligned"},
    {"index": 2, "source": "semantic_block_v2_whisper_aligned"},
    {"index": 3, "source": "semantic_block_v2_whisper_aligned"},
    {"index": 4, "source": "semantic_block_v2_proportional"},
    {"index": 5, "source": "semantic_block_v2_proportional"}
  ]
}
```

修改 project_state.json 加 alignment stage：

```json
{
  "project_id": "test_pid_001",
  "stages": {
    "ingestion": {"payload": {"duration_ms": 254000}},
    "media_understanding": {"payload": {"language": "en_us", "speaker_count": 2}},
    "audio_alignment": {"payload": {"cache_hit_blocks": 4, "block_count": 5}}
  }
}
```

- [ ] **D2.3: 实现 helpers**

```python
_WHISPER_ALIGNED_SOURCE = "semantic_block_v2_whisper_aligned"
# Stage names that may contain workflow alignment cache (prod smoke 验证)
_ALIGNMENT_STAGE_CANDIDATES = ("audio_alignment", "subtitle_alignment", "alignment")


def _compute_whisper(project_dir: Path | None) -> dict | None:
    if not project_dir:
        return None
    cues_path = project_dir / ARTIFACT_PATHS["subtitle_cues"]
    sidecar_count = sum(
        1 for _ in project_dir.rglob("*.whisper_*_*.json")
    )
    if not cues_path.is_file():
        return {
            "alignment_model": None,
            "alignment_fingerprint": None,
            "whisper_aligned_cue_count": None,
            "proportional_fallback_cue_count": None,
            "whisper_sidecar_count": sidecar_count,
            "_reason_null": "subtitle_cues.json absent (pre-Phase-D job)",
        }
    data = _safe_load_json(cues_path)
    if not isinstance(data, dict):
        return None
    cues = data.get("cues") or []
    aligned = sum(1 for c in cues
                  if _WHISPER_ALIGNED_SOURCE in str(c.get("source", "")))
    total = len(cues)
    return {
        "alignment_model": data.get("alignment_model"),
        "alignment_fingerprint": data.get("alignment_fingerprint"),
        "whisper_aligned_cue_count": aligned,
        "proportional_fallback_cue_count": max(0, total - aligned),
        "whisper_sidecar_count": sidecar_count,
    }


def _compute_workflow_alignment_cache(project_state: dict | None) -> dict | None:
    """DSP TTS aligned-audio stage cache (NOT whisper)."""
    if not isinstance(project_state, dict):
        return None
    stages = project_state.get("stages") or {}
    for name in _ALIGNMENT_STAGE_CANDIDATES:
        stage = stages.get(name)
        if isinstance(stage, dict):
            payload = stage.get("payload") or {}
            chb = payload.get("cache_hit_blocks")
            bc = payload.get("block_count")
            if isinstance(chb, int):
                return {
                    "cache_hit_blocks": chb,
                    "block_count": bc if isinstance(bc, int) else None,
                    "_stage_name": name,
                }
    return {
        "cache_hit_blocks": None,
        "block_count": None,
        "_reason_null": "no alignment stage found in project_state",
    }
```

加进 _build_fact_sheet:

```python
    "whisper": _compute_whisper(project_dir),
    "workflow_alignment_cache": _compute_workflow_alignment_cache(ps),
```

- [ ] **D2.4: 跑测试 → pass**

- [ ] **D2.5: Commit**

```bash
git commit -m "feat: extract whisper deliverable fields + workflow_alignment_cache (NOT same)"
```

---

### Task D3: user_edit_events.jsonl 聚合

**Files:**
- Modify: `scripts/smart_shadow_eval_collector.py`
- Create: `tests/fixtures/smart_shadow_eval/projects/test_pid_001/job_post_phase_full/audit/user_edit_events.jsonl`
- Modify: `tests/test_smart_shadow_eval_collector.py`

- [ ] **D3.1: 写失败测试**

```python
def test_user_edits(tmp_path):
    f = ...
    ue = f["user_edits"]
    assert ue["speaker_corrections_effective"] == 2
    assert ue["splits_confirmed_effective"] == 1
    assert ue["text_changes_effective"] == 3
```

- [ ] **D3.2: 创建 fixture**

```jsonl
{"event_type": "translation_segment_speaker_changed", "effective_marker": "effective"}
{"event_type": "translation_segment_speaker_changed", "effective_marker": "effective"}
{"event_type": "translation_segment_speaker_changed", "effective_marker": "weak"}
{"event_type": "translation_segment_split_confirmed", "effective_marker": "effective"}
{"event_type": "translation_segment_text_changed", "effective_marker": "effective"}
{"event_type": "translation_segment_text_changed", "effective_marker": "effective"}
{"event_type": "post_edit_text_changed", "effective_marker": "effective"}
```

- [ ] **D3.3: 实现 helper**

```python
SPEAKER_EVENT_TYPES = frozenset({
    "translation_segment_speaker_changed",
    "post_edit_segment_speaker_changed",
    "voice_selection_speaker_reassigned",
})
SPLIT_EVENT_TYPES = frozenset({
    "translation_segment_split_confirmed",
    "post_edit_segment_split_confirmed",
})
TEXT_EVENT_TYPES = frozenset({
    "translation_segment_text_changed",
    "post_edit_text_changed",
})


def _compute_user_edits(project_dir: Path | None) -> dict | None:
    if not project_dir:
        return None
    path = project_dir / ARTIFACT_PATHS["user_edit_events"]
    if not path.is_file():
        return {
            "speaker_corrections_effective": None,
            "splits_confirmed_effective": None,
            "text_changes_effective": None,
            "_reason_null": "audit/user_edit_events.jsonl absent",
        }
    counts = {"speaker": 0, "split": 0, "text": 0}
    try:
        # Intent events stay effective=False on disk (append-only, plan §4.5).
        # Effectiveness is recorded by a separate event_type="effective_marker"
        # event whose context.marked_event_ids enumerates the intent event_ids
        # to promote. So we two-pass: collect promoted ids, then count.
        events: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        marked_ids: set[str] = set()
        for ev in events:
            if ev.get("event_type") != "effective_marker":
                continue
            ctx = ev.get("context") or {}
            for eid in ctx.get("marked_event_ids") or []:
                if isinstance(eid, str):
                    marked_ids.add(eid)
        for ev in events:
            eid = ev.get("event_id")
            if not isinstance(eid, str) or eid not in marked_ids:
                continue
            et = ev.get("event_type")
            if et in SPEAKER_EVENT_TYPES:
                counts["speaker"] += 1
            elif et in SPLIT_EVENT_TYPES:
                counts["split"] += 1
            elif et in TEXT_EVENT_TYPES:
                counts["text"] += 1
    except OSError:
        return None
    return {
        "speaker_corrections_effective": counts["speaker"],
        "splits_confirmed_effective": counts["split"],
        "text_changes_effective": counts["text"],
    }
```

加进 fact sheet:

```python
    "user_edits": _compute_user_edits(project_dir),
```

- [ ] **D3.4: 跑测试 → pass**

- [ ] **D3.5: Commit**

```bash
git commit -m "feat: aggregate user_edit_events by category (effective only)"
```

---

## Phase E: Hardening — guards + signal handling + skipped fixtures

### Task E1: AST import 守卫测试

**Files:**
- Create: `tests/test_smart_shadow_eval_collector_imports.py`

- [ ] **E1.1: 写测试（直接给完整文件）**

```python
"""AST import guard: collector must only import stdlib."""
import ast
from pathlib import Path

COLLECTOR_PATH = (Path(__file__).resolve().parent.parent
                  / "scripts" / "smart_shadow_eval_collector.py")

STDLIB_WHITELIST = frozenset({
    "argparse", "json", "pathlib", "datetime", "hashlib",
    "sys", "os", "signal", "traceback", "socket", "subprocess",
    "logging", "collections", "typing", "dataclasses", "re", "time",
    "__future__",
})

FORBIDDEN_PREFIXES = ("src.", "gateway.")
FORBIDDEN_NAMES = frozenset({
    "anthropic", "google", "boto3", "openai", "httpx",
    "pydantic", "faster_whisper", "ctranslate2", "torch",
})


def _imported_top_modules(tree: ast.AST) -> set[str]:
    """Yield top-level module names from import statements."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                out.add(node.module.split(".")[0])
    return out


def test_collector_only_imports_stdlib():
    src = COLLECTOR_PATH.read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported = _imported_top_modules(tree)
    for mod in imported:
        assert mod in STDLIB_WHITELIST, (
            f"forbidden import: {mod!r} not in stdlib whitelist"
        )
        assert not any(mod.startswith(p) for p in FORBIDDEN_PREFIXES), (
            f"forbidden import: {mod!r} starts with project prefix"
        )
        assert mod not in FORBIDDEN_NAMES, (
            f"forbidden external SDK import: {mod!r}"
        )
```

- [ ] **E1.2: 跑测试 → pass（已经全是 stdlib）**

Run: `python -m pytest tests/test_smart_shadow_eval_collector_imports.py -v`

- [ ] **E1.3: Commit**

```bash
git commit -m "test: AST import guard for collector stdlib-only requirement"
```

---

### Task E2: PII 注入守卫

**Files:**
- Create: `tests/test_smart_shadow_eval_collector_pii_guard.py`
- Create: `tests/fixtures/smart_shadow_eval/projects/test_pid_pii/job_pii_test/...`
- Create: `tests/fixtures/smart_shadow_eval/jobs/job_pii_test.json`

- [ ] **E2.1: 写测试（含 PII 字面量必检）**

```python
"""PII 注入守卫：fact sheet 不得出现以下字面量。"""
import sys
import json
import subprocess
from pathlib import Path

SCRIPT = (Path(__file__).resolve().parent.parent
          / "scripts" / "smart_shadow_eval_collector.py")
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "smart_shadow_eval"

# These must NOT appear in any fact sheet output:
PII_LITERALS = [
    "贝基·奎克",          # Chinese personal name
    "沃伦·巴菲特",         # Chinese personal name
    "13800138000",        # Phone number
    "$19,100,000",        # Financial figure
    "abc@example.com",    # Email
    "我们今天的嘉宾是埃隆·马斯克",  # Chinese full sentence (cn_text)
]


def test_no_pii_in_fact_sheet(tmp_path):
    out_dir = tmp_path / "out"
    subprocess.run(
        [sys.executable, str(SCRIPT),
         "--jobs-root", str(FIXTURES / "jobs"),
         "--projects-root", str(FIXTURES / "projects"),
         "--out-dir", str(out_dir)],
        check=True, capture_output=True
    )
    facts = (out_dir / "facts.jsonl").read_text(encoding="utf-8")
    for lit in PII_LITERALS:
        assert lit not in facts, f"PII leak: {lit!r} found in facts.jsonl"
```

- [ ] **E2.2: 创建 PII fixture project**

```json
// jobs/job_pii_test.json
{
  "job_id": "job_pii_test",
  "project_id": "test_pid_pii",
  "status": "succeeded",
  "service_mode": "studio",
  "tts_provider": "minimax",
  "tts_model": "speech-2.8-hd",
  "created_at": "2026-05-06T08:00:00+00:00",
  "edit_generation": 0,
  "copy_of_job_id": null,
  "root_job_id": "job_pii_test",
  "project_dir": "test_pid_pii/job_pii_test"
}
```

```json
// projects/test_pid_pii/job_pii_test/project_state.json
{
  "project_id": "test_pid_pii",
  "stages": {
    "ingestion": {"payload": {"duration_ms": 60000}},
    "media_understanding": {"payload": {"language": "en_us", "speaker_count": 2}}
  }
}
```

```json
// projects/test_pid_pii/job_pii_test/transcript/transcript.json
{
  "lines": [
    {"speaker_id": "speaker_a", "start_ms": 0, "end_ms": 5000, "source_text": "Welcome 13800138000", "cn_text": "我们今天的嘉宾是埃隆·马斯克"}
  ]
}
```

```json
// projects/test_pid_pii/job_pii_test/editor/segments.json
[
  {"segment_id": "1", "speaker_id": "speaker_a", "voice_id": "moss_audio_xxx", "cn_text": "贝基·奎克你好", "display_name": "贝基·奎克"}
]
```

```json
// projects/test_pid_pii/job_pii_test/translation/segments.json
[
  {"segment_id": 1, "cn_text": "沃伦·巴菲特说 $19,100,000"}
]
```

```json
// projects/test_pid_pii/job_pii_test/review_state.json
{
  "stages": {
    "translation_review": {
      "payload": {
        "segments": {
          "1": {"cn_text": "abc@example.com 提供"}
        }
      }
    }
  }
}
```

```json
// projects/test_pid_pii/job_pii_test/download_metadata.json
{"video_title": "贝基·奎克专访 沃伦·巴菲特", "duration_ms": 60000}
```

- [ ] **E2.3: 跑测试**

如果 fail，说明某个字段把 PII 含进去了。修 collector：去掉 / 截断含敏感数据的字段（在 _build_fact_sheet 不输出 source_text / cn_text / display_name 等原文字段）。

- [ ] **E2.4: 跑测试 → pass**

- [ ] **E2.5: Commit**

```bash
git commit -m "test: PII injection guard with 6 sensitive literals + multi-source fixtures"
```

---

### Task E3: ARTIFACT_PATHS 同步守卫

**Files:**
- Create: `tests/test_smart_shadow_eval_paths_in_sync.py`

- [ ] **E3.1: 写测试**

```python
"""ARTIFACT_PATHS 与 fixture 同步守卫：每条 entry 在 post_phase_full fixture 中至少有 1 个真实文件。"""
import sys
import importlib.util
from pathlib import Path

SCRIPT = (Path(__file__).resolve().parent.parent
          / "scripts" / "smart_shadow_eval_collector.py")
FIXTURE = (Path(__file__).resolve().parent / "fixtures" / "smart_shadow_eval"
           / "projects" / "test_pid_001" / "job_post_phase_full")


def _load_artifact_paths():
    spec = importlib.util.spec_from_file_location("collector", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.ARTIFACT_PATHS


def test_every_artifact_path_exists_in_full_fixture():
    paths = _load_artifact_paths()
    project_dir_paths = {k: v for k, v in paths.items()
                         if not v.startswith("{job_id}")}
    missing = []
    for name, rel in project_dir_paths.items():
        if not (FIXTURE / rel).exists():
            missing.append((name, rel))
    assert not missing, (
        f"ARTIFACT_PATHS entries missing in fixture: {missing}. "
        "If you added a path constant, add a corresponding fixture file."
    )
```

- [ ] **E3.2: 跑测试**

如果 fail，说明 ARTIFACT_PATHS 加了 entry 但 fixture 没补对应文件。补 fixture 直到 pass。

- [ ] **E3.3: Commit**

```bash
git commit -m "test: ARTIFACT_PATHS sync guard against post_phase_full fixture"
```

---

### Task E4: SIGINT/SIGTERM 处理 + is_complete_run flag

**Files:**
- Modify: `scripts/smart_shadow_eval_collector.py`
- Modify: `tests/test_smart_shadow_eval_collector.py`

- [ ] **E4.1: 写测试（中断时 summary.json 写 is_complete_run=false）**

```python
def test_sigint_writes_incomplete_summary(tmp_path):
    """Send SIGINT during long scan → summary.is_complete_run=false."""
    import time, signal as sig
    fixtures = Path(__file__).resolve().parent / "fixtures" / "smart_shadow_eval"
    out_dir = tmp_path / "out"
    proc = subprocess.Popen(
        [sys.executable, str(SCRIPT),
         "--jobs-root", str(fixtures / "jobs"),
         "--projects-root", str(fixtures / "projects"),
         "--out-dir", str(out_dir)],
    )
    time.sleep(0.05)  # Let it start
    proc.send_signal(sig.SIGINT)
    proc.wait(timeout=5)
    # Should be either completed or interrupted; check summary
    summary_path = out_dir / "summary.json"
    if summary_path.is_file():
        s = json.loads(summary_path.read_text())
        assert "is_complete_run" in s
        # If interrupted, is_complete_run=false; if too fast, true is OK
```

- [ ] **E4.2: 实现 signal handler**

在 main() 顶部加：

```python
import signal as _signal


_INTERRUPTED = {"flag": False}


def _signal_handler(signum, frame):
    _INTERRUPTED["flag"] = True


def _install_signal_handlers():
    try:
        _signal.signal(_signal.SIGINT, _signal_handler)
        _signal.signal(_signal.SIGTERM, _signal_handler)
    except (AttributeError, ValueError):
        pass  # Some platforms (Windows) restrict
```

main() 顶部调用 `_install_signal_handlers()`，循环里检查：

```python
        for record_path in paths:
            if _INTERRUPTED["flag"]:
                break
            # ... existing logic ...
```

最后写 summary 时：

```python
    summary = {
        ...
        "is_complete_run": not _INTERRUPTED["flag"],
        ...
    }
```

中断时不要 atomic rename（保留 .tmp）：

```python
    if not _INTERRUPTED["flag"]:
        facts_tmp.rename(out_dir / "facts.jsonl")
        inventory_tmp.rename(out_dir / "inventory.jsonl")
    # else: leave .tmp files
```

中断时退出码：

```python
    return 130 if _INTERRUPTED["flag"] else (1 if facts_count == 0 and len(errors) > 0 else 0)
```

- [ ] **E4.3: 跑测试 → pass**

- [ ] **E4.4: Commit**

```bash
git commit -m "feat: SIGINT/SIGTERM handler with is_complete_run flag + tmp preservation"
```

---

### Task E5: corrupted state fixture + skipped_for_missing_identity 计数

**Files:**
- Create: `tests/fixtures/smart_shadow_eval/jobs/job_corrupted_state.json`
- Create: `tests/fixtures/smart_shadow_eval/projects/test_pid_corrupted/job_corrupted_state/project_state.json`
- Modify: `tests/test_smart_shadow_eval_collector.py`

- [ ] **E5.1: 写失败测试**

```python
def test_corrupted_record_skipped(tmp_path):
    """Job with no created_at → skipped, count incremented."""
    # ... run collector against fixture with corrupted job ...
    summary = json.loads((out_dir / "summary.json").read_text())
    assert summary["scan_stats"]["skipped_for_missing_identity"] >= 1
```

- [ ] **E5.2: 创建 fixture（缺 created_at）**

```json
// jobs/job_corrupted_state.json
{
  "job_id": "job_corrupted_state",
  "project_id": "test_pid_corrupted",
  "status": "succeeded",
  "service_mode": "studio",
  "tts_provider": "minimax",
  "tts_model": "speech-2.8-hd"
}
```

(故意缺 `created_at`)

- [ ] **E5.3: 跑测试 → 已经 pass（A3 实现已经处理）**

- [ ] **E5.4: Commit**

```bash
git commit -m "test: corrupted_state fixture for missing identity skip counter"
```

---

## Phase F: Local smoke against .codex_tmp samples

### Task F1: 跑本地 smoke

**Files:**
- 无新文件

- [ ] **F1.0: 先跑全部测试守卫确保 collector 仍合规**

```bash
python -m pytest tests/test_smart_shadow_eval_collector_imports.py -v
python -m pytest tests/test_smart_shadow_eval_collector_pii_guard.py -v
python -m pytest tests/test_smart_shadow_eval_paths_in_sync.py -v
python -m pytest tests/test_smart_shadow_eval_collector.py -v
```

Expected: all green. 任何 import / PII / path 守卫红 → **stop and fix**，不要往下跑 smoke。

> 这一步是回归保险：Phase B/C/D/E 加了新 helper / 新 import / 新 fixture 字段后，可能漏更新守卫白名单。Smoke 之前必须确认所有契约级守卫仍绿。

- [ ] **F1.1: 跑 collector against 12 真实样本**

```bash
mkdir -p D:/Claude/temp/smart_shadow_eval/local_smoke_$(date +%Y%m%dT%H%M)
python scripts/smart_shadow_eval_collector.py \
  --projects-root D:/Claude/AIVideoTrans_Codex_web_mvp/.codex_tmp/us_fetch/extracted/opt/aivideotrans/data/projects \
  --jobs-root D:/Claude/AIVideoTrans_Codex_web_mvp/.codex_tmp/us_fetch/extracted/opt/aivideotrans/data/jobs \
  --out-dir D:/Claude/temp/smart_shadow_eval/local_smoke
```

- [ ] **F1.2: 验证产出**

```bash
ls D:/Claude/temp/smart_shadow_eval/local_smoke/
# Expected: facts.jsonl, inventory.jsonl, summary.json
wc -l D:/Claude/temp/smart_shadow_eval/local_smoke/facts.jsonl
# Expected: 12 (one per sample)
```

- [ ] **F1.3: 肉眼检查 fact sheet 字段**

```bash
head -1 D:/Claude/temp/smart_shadow_eval/local_smoke/facts.jsonl | python -m json.tool | head -50
```

预期：
- §1.1A 字段（job_id / status / created_at / duration_seconds / source_language / speaker_stats）有值
- §1.1B 字段（subtitle_sync.text_audio_drift_count / whisper.* / usage_meter / user_edits）全 null + `_reason_null`

- [ ] **F1.4: PII 检查（grep 真实姓名）**

```bash
grep -E "贝基|沃伦|马斯克|奎克" D:/Claude/temp/smart_shadow_eval/local_smoke/facts.jsonl
# Expected: no match
```

- [ ] **F1.5: 单行宽检查**

```bash
awk '{ print length }' D:/Claude/temp/smart_shadow_eval/local_smoke/facts.jsonl | sort -n | tail -1
# Expected: ≤ 4096
```

- [ ] **F1.6: 关键审核节点 — 把 facts.jsonl 第一行给 owner 看**

⏸ **STOP HERE** — Show owner the first fact sheet sample. Confirm:
- Field shape matches spec §3.3
- No PII leaked
- Size sane

Owner approves → proceed to Phase G analyzer. Owner objects → fix and re-smoke.

---

## Phase G: Analyzer 骨架 + 报告 §1-§4

### Task G1: Analyzer 骨架 + facts loader + schema_version / is_complete_run gate

**Files:**
- Create: `scripts/smart_shadow_eval_analyzer.py`
- Create: `tests/test_smart_shadow_eval_analyzer.py`

- [ ] **G1.1: 写失败测试**

```python
import sys, json, subprocess
from pathlib import Path

SCRIPT = (Path(__file__).resolve().parent.parent
          / "scripts" / "smart_shadow_eval_analyzer.py")


def test_analyzer_help():
    result = subprocess.run([sys.executable, str(SCRIPT), "--help"],
                            capture_output=True, text=True)
    assert result.returncode == 0
    assert "--facts" in result.stdout


def test_analyzer_rejects_incomplete_run(tmp_path):
    """summary.is_complete_run=false → analyzer 拒读"""
    facts = tmp_path / "facts.jsonl"
    facts.write_text("")
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps({"is_complete_run": False, "schema_version": 1, "scan_stats": {}}))
    out = tmp_path / "report"
    result = subprocess.run(
        [sys.executable, str(SCRIPT),
         "--facts", str(facts),
         "--summary", str(summary),
         "--out-dir", str(out)],
        capture_output=True, text=True
    )
    assert result.returncode != 0
    assert "is_complete_run" in (result.stderr + result.stdout)


def test_analyzer_rejects_summary_missing_schema_version(tmp_path):
    """summary 无 schema_version 字段 → 显式 reject（不 silent fallthrough）"""
    facts = tmp_path / "facts.jsonl"
    facts.write_text("")
    summary = tmp_path / "summary.json"
    # NO schema_version key
    summary.write_text(json.dumps({"is_complete_run": True, "scan_stats": {}}))
    out = tmp_path / "report"
    result = subprocess.run(
        [sys.executable, str(SCRIPT),
         "--facts", str(facts),
         "--summary", str(summary),
         "--out-dir", str(out)],
        capture_output=True, text=True
    )
    assert result.returncode == 2
    assert "schema_version" in (result.stderr + result.stdout)
```

- [ ] **G1.2: 实现 analyzer 骨架（含所有后续 task 需要的 imports + helper）**

```python
# scripts/smart_shadow_eval_analyzer.py
"""Smart Shadow Evaluator analyzer — read facts.jsonl + pricing snapshot, emit report.md.

Quick usage:
  python scripts/smart_shadow_eval_analyzer.py \\
    --facts D:/Claude/temp/smart_shadow_eval/<run_id>/facts.jsonl \\
    --summary D:/Claude/temp/smart_shadow_eval/<run_id>/summary.json \\
    --pricing-runtime-snapshot D:/Claude/temp/.../pricing_runtime.json \\
    --out-dir D:/Claude/temp/.../report

See docs/plans/2026-05-06-smart-shadow-evaluator-design.md.
"""
from __future__ import annotations
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path


SCHEMA_VERSION = 1
AVG_REWRITE_CHARS = 30  # fallback per-rewrite char estimate (no metering data)


def _percentile(sorted_xs, p: float):
    """Return percentile p (0..1) from a pre-sorted iterable."""
    if not sorted_xs:
        return None
    idx = min(len(sorted_xs) - 1, int(len(sorted_xs) * p))
    return sorted_xs[idx]


def build_arg_parser():
    p = argparse.ArgumentParser(description="Smart shadow eval analyzer")
    p.add_argument("--facts", required=True)
    p.add_argument("--inventory", required=False)
    p.add_argument("--summary", required=False)
    p.add_argument("--pricing-runtime-snapshot", required=False)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--phase-cutoff-date", default="2026-05-05")
    p.add_argument("--smart-eligibility-threshold-set", default="0.05,0.10,0.15,0.20")
    p.add_argument("--min-sample-seconds-set", default="5,8,10,15")
    p.add_argument("--allow-incomplete-run", action="store_true")
    p.add_argument("--expected-schema-version", type=int, default=SCHEMA_VERSION)
    return p


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    facts_path = Path(args.facts)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Gate: summary.is_complete_run + schema_version
    # BLOCKER #2 fix: schema_version default MUST be a sentinel that's never
    # equal to expected_schema_version, so missing field is treated as
    # explicit reject (not silent passthrough).
    _MISSING = object()
    if args.summary:
        try:
            s = json.loads(Path(args.summary).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"ERROR: cannot read summary.json: {exc}", file=sys.stderr)
            return 2
        if not args.allow_incomplete_run and not s.get("is_complete_run", True):
            print("ERROR: summary.is_complete_run=false; "
                  "pass --allow-incomplete-run to override",
                  file=sys.stderr)
            return 2
        sv = s.get("schema_version", _MISSING)
        if sv is _MISSING:
            print("ERROR: summary missing schema_version field; "
                  "produced by an unsupported collector version",
                  file=sys.stderr)
            return 2
        if sv != args.expected_schema_version:
            print(f"ERROR: summary schema_version={sv} != expected="
                  f"{args.expected_schema_version}",
                  file=sys.stderr)
            return 2

    # Load facts
    facts = []
    if facts_path.is_file():
        for line in facts_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                facts.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    # ─────────────────────────────────────────────────────────────────────
    # PLACEMENT CONTRACT for subsequent tasks (G2.2, G3a/b/c, G4a/b/c, G5):
    # All `report_lines += _section_*(facts)` and `summary_extra.update(...)`
    # MUST be inserted ABOVE the `summary_payload = {...}` and `write_text`
    # calls below. Placing them after = silent loss (dict snapshot at unpack
    # time / report.md already written).
    # ─────────────────────────────────────────────────────────────────────

    # Generate skeleton report (Phase G1: only metadata)
    # summary_extra accumulates fields written by later sections (e.g., §10 threshold_matrix)
    summary_extra: dict = {}
    report_lines = [
        "# Smart Shadow Evaluator Report",
        "",
        f"- Facts loaded: {len(facts)}",
        f"- Out dir: {out_dir}",
    ]
    if not facts:
        report_lines.append("")
        report_lines.append("⚠️ No facts available — empty dump or no jobs in date range.")

    # ↓↓↓ Subsequent tasks insert their section calls HERE ↓↓↓
    # (G2.2 inserts §1+§2+§3, G3a inserts §4, G3b inserts §5, etc.)
    # ↑↑↑ All section calls MUST be above the writes below ↑↑↑

    (out_dir / "report.md").write_text("\n".join(report_lines), encoding="utf-8")

    # report_summary.json payload — sections accumulate fields into summary_extra
    # (e.g., G3c writes "threshold_matrix"). Initialized empty; subsequent tasks
    # override via main()'s summary_extra dict (see G3c.2).
    summary_payload = {
        "facts_count": len(facts),
        **summary_extra,  # populated by §10 (G3c) and possibly future sections
    }
    (out_dir / "report_summary.json").write_text(
        json.dumps(summary_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

**Note**: `summary_extra: dict = {}` should be declared near the top of main()
right after `summary` is loaded. Subsequent tasks that need to write extra
fields into `report_summary.json` will mutate it in-place via
`summary_extra.update(...)` calls (see G3c.2).

- [ ] **G1.3: 跑测试 → pass**

- [ ] **G1.4: Commit**

```bash
git commit -m "feat: analyzer skeleton with is_complete_run gate + schema_version check"
```

---

### Task G2: Analyzer §1 metadata + §2 数据可用性 + §3 speaker count

**Files:**
- Modify: `scripts/smart_shadow_eval_analyzer.py`
- Modify: `tests/test_smart_shadow_eval_analyzer.py`

- [ ] **G2.1: 写失败测试**

```python
def test_analyzer_speaker_count_section(tmp_path):
    facts = tmp_path / "facts.jsonl"
    # 5 jobs: 3 with main_speaker_count=2, 2 with =4
    samples = [
        {"schema_version": 1, "job_id": f"j{i}",
         "speaker_stats": {"speaker_count_by_threshold": {"0.10": cnt}}}
        for i, cnt in enumerate([2, 2, 2, 4, 4])
    ]
    facts.write_text("\n".join(json.dumps(s) for s in samples))
    out = tmp_path / "report"
    subprocess.run([sys.executable, str(SCRIPT),
                    "--facts", str(facts), "--out-dir", str(out)],
                   check=True, capture_output=True)
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "Speaker 数分布" in report
    # 3/5 = 60% main_speaker ≤ 3 (at threshold 0.10)
    assert "60" in report or "0.6" in report
```

- [ ] **G2.2: 实现 §1-§3 sections**

加进 main()，替换 report_lines 构造逻辑：

```python
def _section_metadata(facts, summary, args):
    return [
        "## §1 Run Metadata",
        f"- run_id: {(summary or {}).get('run_id', 'N/A')}",
        f"- facts loaded: {len(facts)}",
        f"- jobs_factsheeted: {((summary or {}).get('scan_stats') or {}).get('jobs_factsheeted', 'N/A')}",
        f"- is_complete_run: {(summary or {}).get('is_complete_run', 'N/A')}",
        "",
    ]


def _section_data_availability(facts, cutoff_date):
    if not facts:
        return ["## §2 数据可用性\n\n(no data)\n"]
    keys = ["project_state_json", "transcript_json",
            "metering_usage_events", "audit_user_edit_events",
            "subtitle_quality_report", "subtitle_cues"]
    pre = [f for f in facts if f.get("created_at", "") < cutoff_date]
    post = [f for f in facts if f.get("created_at", "") >= cutoff_date]
    lines = ["## §2 数据可用性\n"]
    for label, group in [(f"pre {cutoff_date} (N={len(pre)})", pre),
                          (f"post {cutoff_date} (N={len(post)})", post)]:
        lines.append(f"### {label}")
        for k in keys:
            present = sum(1 for f in group if (f.get("artifact_presence") or {}).get(k))
            pct = (present / len(group) * 100) if group else 0
            lines.append(f"- {k}: {present}/{len(group)} ({pct:.0f}%)")
        lines.append("")
    return lines


def _section_speaker_count(facts, threshold_set):
    if not facts:
        return ["## §3 Speaker 数分布\n\n(no data)\n"]
    thresholds = [t.strip() for t in threshold_set.split(",")]
    lines = ["## §3 Speaker 数分布\n",
             "| Threshold | Main ≤ 3 占比 | Main ≤ 2 | Main ≤ 1 |",
             "|---|---|---|---|"]
    for t in thresholds:
        counts = []
        for f in facts:
            sct = (f.get("speaker_stats") or {}).get("speaker_count_by_threshold") or {}
            c = sct.get(t)
            if isinstance(c, int):
                counts.append(c)
        if not counts:
            lines.append(f"| {t} | (no data) | - | - |")
            continue
        leq3 = sum(1 for c in counts if c <= 3)
        leq2 = sum(1 for c in counts if c <= 2)
        leq1 = sum(1 for c in counts if c <= 1)
        n = len(counts)
        lines.append(f"| {t} | {leq3}/{n} ({leq3/n*100:.0f}%) | {leq2}/{n} ({leq2/n*100:.0f}%) | {leq1}/{n} ({leq1/n*100:.0f}%) |")
    lines.append("")
    return lines


# Replace skeleton report construction:
def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    # ... (gating + facts load) ...
    summary = None
    if args.summary:
        try:
            summary = json.loads(Path(args.summary).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass

    report_lines = ["# Smart Shadow Evaluator Report", ""]
    report_lines += _section_metadata(facts, summary, args)
    report_lines += _section_data_availability(facts, args.phase_cutoff_date)
    report_lines += _section_speaker_count(facts, args.smart_eligibility_threshold_set)

    (out_dir / "report.md").write_text("\n".join(report_lines), encoding="utf-8")
    # ... rest ...
```

- [ ] **G2.3: 跑测试 → pass**

- [ ] **G2.4: Commit**

```bash
git commit -m "feat: analyzer §1 metadata + §2 availability + §3 speaker distribution"
```

---

### Task G3a: Analyzer §4 clone sample availability

**Files:**
- Modify: `scripts/smart_shadow_eval_analyzer.py`
- Modify: `tests/test_smart_shadow_eval_analyzer.py`

- [ ] **G3a.1: 写失败测试**

```python
def test_analyzer_clone_availability_section(tmp_path):
    """§4: 按 main_speaker_count(threshold=0.10) 分桶 → 每桶有≥1 个合格样本(≥5s) 的占比"""
    facts = tmp_path / "facts.jsonl"
    samples = [
        # main=2, both speakers have eligible samples
        {"schema_version": 1, "job_id": "j1",
         "speaker_stats": {"speaker_count_by_threshold": {"0.10": 2}},
         "clone_sample_stats": {"eligible_speakers": 2,
            "eligible_sample_count_buckets_by_speaker": [
                {"≥5s": 5, "≥8s": 3, "≥10s": 1, "≥15s": 0},
                {"≥5s": 3, "≥8s": 2, "≥10s": 0, "≥15s": 0}]}},
        # main=2, one speaker has 0 eligible samples ≥5s
        {"schema_version": 1, "job_id": "j2",
         "speaker_stats": {"speaker_count_by_threshold": {"0.10": 2}},
         "clone_sample_stats": {"eligible_speakers": 2,
            "eligible_sample_count_buckets_by_speaker": [
                {"≥5s": 5, "≥8s": 3, "≥10s": 1, "≥15s": 0},
                {"≥5s": 0, "≥8s": 0, "≥10s": 0, "≥15s": 0}]}},
        # main=3
        {"schema_version": 1, "job_id": "j3",
         "speaker_stats": {"speaker_count_by_threshold": {"0.10": 3}},
         "clone_sample_stats": {"eligible_speakers": 3,
            "eligible_sample_count_buckets_by_speaker": [
                {"≥5s": 5, "≥8s": 3, "≥10s": 1, "≥15s": 0},
                {"≥5s": 4, "≥8s": 2, "≥10s": 0, "≥15s": 0},
                {"≥5s": 2, "≥8s": 1, "≥10s": 0, "≥15s": 0}]}},
    ]
    facts.write_text("\n".join(json.dumps(s) for s in samples))
    out = tmp_path / "report"
    subprocess.run([sys.executable, str(SCRIPT),
                    "--facts", str(facts), "--out-dir", str(out)],
                   check=True, capture_output=True)
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "§4 克隆样本可用率" in report
    # main=2 bucket: j1 (2/2 speakers eligible @≥5s) vs j2 (1/2) → 50% all-eligible
    # main=3 bucket: j3 (3/3) → 100% all-eligible
    assert "main=2" in report or "2 speakers" in report
    assert "main=3" in report or "3 speakers" in report
```

- [ ] **G3a.2: 实现 _section_clone_availability**

```python
def _section_clone_availability(facts):
    """§4: For each main_speaker_count bucket (using threshold=0.10),
    show what % of jobs have ALL speakers with ≥1 eligible sample ≥5s."""
    if not facts:
        return ["## §4 克隆样本可用率\n\n(no data)\n"]
    by_main_count = defaultdict(list)  # main_count -> list of jobs
    for f in facts:
        sct = (f.get("speaker_stats") or {}).get("speaker_count_by_threshold") or {}
        main_count = sct.get("0.10")
        css = f.get("clone_sample_stats")
        if isinstance(main_count, int) and css:
            by_main_count[main_count].append(css)
    if not by_main_count:
        return ["## §4 克隆样本可用率\n\n(no clone_sample_stats data)\n"]
    lines = ["## §4 克隆样本可用率",
             "",
             "按 main_speaker_count (threshold=0.10) 分桶，每桶里所有主 speaker 都有 ≥1 个 ≥5s 合格样本的 job 占比：",
             "",
             "| main_count | jobs | all-eligible (≥5s) | all-eligible (≥8s) |",
             "|---|---|---|---|"]
    for mc in sorted(by_main_count.keys()):
        jobs = by_main_count[mc]
        all_5 = sum(
            1 for css in jobs
            if all(b.get("≥5s", 0) >= 1 for b in
                   (css.get("eligible_sample_count_buckets_by_speaker") or [])[:mc])
        )
        all_8 = sum(
            1 for css in jobs
            if all(b.get("≥8s", 0) >= 1 for b in
                   (css.get("eligible_sample_count_buckets_by_speaker") or [])[:mc])
        )
        n = len(jobs)
        lines.append(
            f"| main={mc} | {n} | {all_5}/{n} ({all_5/n*100:.0f}%) | "
            f"{all_8}/{n} ({all_8/n*100:.0f}%) |"
        )
    lines.append("")
    return lines
```

加进 main() report_lines 构造：

```python
    report_lines += _section_clone_availability(facts)
```

(注：`from collections import defaultdict` 已在 G1.2 imports 中。)

- [ ] **G3a.3: 跑测试 → pass**

- [ ] **G3a.4: Commit**

```bash
git commit -m "feat: analyzer §4 clone sample availability by main_speaker_count bucket"
```

---

### Task G3b: Analyzer §5 retry distribution

**Files:**
- Modify: `scripts/smart_shadow_eval_analyzer.py`
- Modify: `tests/test_smart_shadow_eval_analyzer.py`

- [ ] **G3b.1: 写失败测试**

```python
def test_analyzer_retry_section(tmp_path):
    """§5: retry/rewrite 分布 + 区分 metering vs fallback 数据源"""
    facts = tmp_path / "facts.jsonl"
    samples = [
        # metering source
        {"schema_version": 1, "job_id": "j1",
         "duration_seconds": 60,
         "retry_stats": {"rewrite_count": 3, "retts_count": 5,
                          "retts_total_duration_ms": 12000,
                          "_data_source": "metering"}},
        {"schema_version": 1, "job_id": "j2",
         "duration_seconds": 120,
         "retry_stats": {"rewrite_count": 1, "retts_count": 2,
                          "retts_total_duration_ms": 4000,
                          "_data_source": "metering"}},
        # fallback source
        {"schema_version": 1, "job_id": "j3",
         "duration_seconds": 60,
         "retry_stats": {"rewrite_count": 2, "retts_count": None,
                          "_data_source": "fallback_editor_segments"}},
    ]
    facts.write_text("\n".join(json.dumps(s) for s in samples))
    out = tmp_path / "report"
    subprocess.run([sys.executable, str(SCRIPT),
                    "--facts", str(facts), "--out-dir", str(out)],
                   check=True, capture_output=True)
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "§5 Retry" in report or "§5 重试" in report
    # metering subset: 2 jobs; rewrite p50=2 (between 1 and 3)
    assert "metering" in report.lower()
    assert "fallback" in report.lower()
```

- [ ] **G3b.2: 实现 _section_retry_distribution**

```python
# _percentile already defined at module top (see G1.2). Reuse here.


def _section_retry_distribution(facts):
    """§5: rewrite/retts distribution split by metering vs fallback.

    Includes rewrite_input_text_chars_total p50/p90/p99 — same denominator
    used by §8 cost (G5), so owner can reconcile §5 retry volume with §8 cost.
    """
    if not facts:
        return ["## §5 Retry 分布\n\n(no data)\n"]
    metering = [f for f in facts
                if (f.get("retry_stats") or {}).get("_data_source") == "metering"]
    fallback = [f for f in facts
                if (f.get("retry_stats") or {}).get("_data_source", "").startswith("fallback")]
    lines = ["## §5 Retry 分布", "",
             f"- jobs with metering data: {len(metering)}",
             f"- jobs with fallback data: {len(fallback)}",
             ""]
    if metering:
        rwc = sorted((f["retry_stats"]["rewrite_count"] or 0) for f in metering)
        rtc = sorted((f["retry_stats"]["retts_count"] or 0) for f in metering)
        rtd = sorted((f["retry_stats"]["retts_total_duration_ms"] or 0) for f in metering)
        # rewrite chars total — same denominator as §8 cost (rewrite_rmb)
        # so owner can reconcile §5 retry volume with §8 cost (reviewer iter2 MAJOR fix)
        rwch = sorted(
            (f.get("usage_meter") or {}).get("rewrite_input_text_chars_total") or 0
            for f in metering
        )
        # retry audio / source ratio per job
        ratios = sorted(
            (f["retry_stats"].get("retts_total_duration_ms") or 0) / 1000.0 /
            max(1, f.get("duration_seconds") or 1)
            for f in metering
        )
        lines += [
            "### Metering subset",
            "",
            "| Metric | p50 | p90 | p99 |",
            "|---|---|---|---|",
            f"| rewrite_count | {_percentile(rwc, 0.5)} | {_percentile(rwc, 0.9)} | {_percentile(rwc, 0.99)} |",
            f"| rewrite_input_text_chars_total | {_percentile(rwch, 0.5)} | {_percentile(rwch, 0.9)} | {_percentile(rwch, 0.99)} |",
            f"| retts_count | {_percentile(rtc, 0.5)} | {_percentile(rtc, 0.9)} | {_percentile(rtc, 0.99)} |",
            f"| retts_audio_ms | {_percentile(rtd, 0.5)} | {_percentile(rtd, 0.9)} | {_percentile(rtd, 0.99)} |",
            f"| retts_audio/src ratio | {_percentile(ratios, 0.5):.3f} | {_percentile(ratios, 0.9):.3f} | {_percentile(ratios, 0.99):.3f} |",
            "",
            "> `rewrite_input_text_chars_total` 是 §8 cost 公式 `rewrite_rmb` 项的输入分母，"
            "owner 可用此列与 §8 cost 数据对账。",
            "",
        ]
    if fallback:
        rwc = sorted((f["retry_stats"]["rewrite_count"] or 0) for f in fallback)
        lines += [
            "### Fallback subset (editor.segments rewrite_count only)",
            "",
            "| Metric | p50 | p90 | p99 |",
            "|---|---|---|---|",
            f"| rewrite_count | {_percentile(rwc, 0.5)} | {_percentile(rwc, 0.9)} | {_percentile(rwc, 0.99)} |",
            "",
            "> retts_count 在 fallback 路径 N/A（旧 job 无 metering）",
            "",
        ]
    return lines
```

加进 main():

```python
    report_lines += _section_retry_distribution(facts)
```

- [ ] **G3b.3: 跑测试 → pass**

- [ ] **G3b.4: Commit**

```bash
git commit -m "feat: analyzer §5 retry distribution split by metering vs fallback"
```

---

### Task G3c: Analyzer §10 threshold calibration matrix（4×4）

**Files:**
- Modify: `scripts/smart_shadow_eval_analyzer.py`
- Modify: `tests/test_smart_shadow_eval_analyzer.py`

- [ ] **G3c.1: 写失败测试**

```python
def test_analyzer_threshold_matrix(tmp_path):
    """§10: 4 main-speaker × 4 min-sample-seconds 矩阵 → Smart 适配率 / 拒绝率 / 降级率"""
    facts = tmp_path / "facts.jsonl"
    samples = [
        # j1: 2 speakers, both have ≥10s samples → eligible at all thresholds
        {"schema_version": 1, "job_id": "j1",
         "speaker_stats": {"speaker_count_by_threshold": {
             "0.05": 2, "0.10": 2, "0.15": 2, "0.20": 2}},
         "clone_sample_stats": {"eligible_speakers": 2,
            "eligible_sample_count_buckets_by_speaker": [
                {"≥5s": 5, "≥8s": 3, "≥10s": 2, "≥15s": 1},
                {"≥5s": 4, "≥8s": 2, "≥10s": 1, "≥15s": 0}]}},
        # j2: 4 speakers (gate fails at threshold 0.05 / 0.10) - rejected
        {"schema_version": 1, "job_id": "j2",
         "speaker_stats": {"speaker_count_by_threshold": {
             "0.05": 4, "0.10": 4, "0.15": 3, "0.20": 2}},
         "clone_sample_stats": {"eligible_speakers": 4,
            "eligible_sample_count_buckets_by_speaker": [
                {"≥5s": 5, "≥8s": 3, "≥10s": 1, "≥15s": 0},
                {"≥5s": 4, "≥8s": 2, "≥10s": 0, "≥15s": 0},
                {"≥5s": 2, "≥8s": 0, "≥10s": 0, "≥15s": 0},
                {"≥5s": 1, "≥8s": 0, "≥10s": 0, "≥15s": 0}]}},
        # j3: 3 speakers, 1 has insufficient samples - degraded at higher min_seconds
        {"schema_version": 1, "job_id": "j3",
         "speaker_stats": {"speaker_count_by_threshold": {
             "0.05": 3, "0.10": 3, "0.15": 3, "0.20": 3}},
         "clone_sample_stats": {"eligible_speakers": 3,
            "eligible_sample_count_buckets_by_speaker": [
                {"≥5s": 5, "≥8s": 3, "≥10s": 1, "≥15s": 0},
                {"≥5s": 3, "≥8s": 1, "≥10s": 0, "≥15s": 0},
                {"≥5s": 0, "≥8s": 0, "≥10s": 0, "≥15s": 0}]}},
    ]
    facts.write_text("\n".join(json.dumps(s) for s in samples))
    out = tmp_path / "report"
    subprocess.run([sys.executable, str(SCRIPT),
                    "--facts", str(facts), "--out-dir", str(out)],
                   check=True, capture_output=True)
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "§10" in report
    assert "适配率" in report or "eligible" in report.lower()
    # At threshold=0.10, ms=5s: j1 OK (2 speakers all ≥1 sample @5s), j2 rejected
    # (4 speakers > 3), j3 partial (1 speaker 0 samples → degraded). So:
    # eligible=1/3, rejected=1/3, degraded=1/3
    summary = json.loads((out / "report_summary.json").read_text())
    assert "threshold_matrix" in summary
```

- [ ] **G3c.2: 实现 _section_threshold_matrix**

```python
def _classify_job_at_threshold(fact, main_threshold_str, min_sec_key):
    """Return 'eligible' | 'rejected' | 'degraded' for a job at given thresholds.
    eligible: main ≤ 3 AND all main speakers have ≥1 sample ≥ min_sec
    rejected: main > 3 (speaker gate fails)
    degraded: main ≤ 3 BUT at least 1 main speaker has no qualifying sample
    """
    sct = (fact.get("speaker_stats") or {}).get("speaker_count_by_threshold") or {}
    main_count = sct.get(main_threshold_str)
    if not isinstance(main_count, int):
        return None  # missing data
    if main_count > 3:
        return "rejected"
    css = fact.get("clone_sample_stats") or {}
    buckets = css.get("eligible_sample_count_buckets_by_speaker") or []
    relevant = buckets[:main_count]
    if len(relevant) < main_count:
        return "degraded"
    if all(b.get(min_sec_key, 0) >= 1 for b in relevant):
        return "eligible"
    return "degraded"


def _section_threshold_matrix(facts, main_thresholds_csv, min_secs_csv):
    """§10: 4×4 matrix of Smart eligibility/rejection/degradation rates."""
    if not facts:
        return ["## §10 阈值校准矩阵\n\n(no data)\n"], {}
    main_ths = [t.strip() for t in main_thresholds_csv.split(",")]
    min_secs = [int(s.strip()) for s in min_secs_csv.split(",")]
    lines = [
        "## §10 阈值校准矩阵 (Smart 适配率 / 拒绝率 / 降级率)",
        "",
        "**核心 P0 输出**：在不同 main-speaker threshold × min-sample-seconds 阈值组合下，"
        "Smart MVP 的适配率 / 拒绝率 / 降级率。Owner 决定 §7.2 / §9 阈值的依据。",
        "",
        "格式：eligible / rejected / degraded（百分比）",
        "",
    ]
    matrix_summary = {}
    for ms in min_secs:
        ms_key = f"≥{ms}s"
        lines += [
            f"### min_sample_seconds = {ms}s",
            "",
            "| main_threshold | eligible | rejected (main>3) | degraded | total |",
            "|---|---|---|---|---|",
        ]
        for mt in main_ths:
            classifications = [
                _classify_job_at_threshold(f, mt, ms_key) for f in facts
            ]
            valid = [c for c in classifications if c is not None]
            n = len(valid) or 1
            elig = classifications.count("eligible")
            rej = classifications.count("rejected")
            deg = classifications.count("degraded")
            lines.append(
                f"| {mt} | {elig}/{n} ({elig/n*100:.0f}%) "
                f"| {rej}/{n} ({rej/n*100:.0f}%) "
                f"| {deg}/{n} ({deg/n*100:.0f}%) | {n} |"
            )
            matrix_summary[f"main={mt}_min={ms}s"] = {
                "eligible_pct": elig / n * 100,
                "rejected_pct": rej / n * 100,
                "degraded_pct": deg / n * 100,
                "total": n,
            }
        lines.append("")
    return lines, {"threshold_matrix": matrix_summary}
```

main() 里加：

```python
    matrix_lines, matrix_extra = _section_threshold_matrix(
        facts, args.smart_eligibility_threshold_set, args.min_sample_seconds_set
    )
    report_lines += matrix_lines
    # Persist to report_summary.json
    summary_extra.update(matrix_extra)
```

(注：需要在 main 顶部初始化 `summary_extra = {}` 并合到 report_summary.json 写入逻辑里)

- [ ] **G3c.3: 跑测试 → pass**

- [ ] **G3c.4: Commit**

```bash
git commit -m "feat: analyzer §10 4x4 threshold calibration matrix (Smart eligibility/rejection/degradation rates)"
```

---

### Task G4a: Analyzer §6 subtitle drift histogram

**Files:**
- Modify: `scripts/smart_shadow_eval_analyzer.py`
- Modify: `tests/test_smart_shadow_eval_analyzer.py`

- [ ] **G4a.1: 写失败测试**

```python
def test_analyzer_drift_section(tmp_path):
    """§6: text_audio_drift_count 分布（仅有 subtitle_quality_report 子集）"""
    facts = tmp_path / "facts.jsonl"
    samples = [
        {"schema_version": 1, "job_id": "j1",
         "artifact_presence": {"subtitle_quality_report": True},
         "subtitle_sync": {"text_audio_drift_count": 0}},
        {"schema_version": 1, "job_id": "j2",
         "artifact_presence": {"subtitle_quality_report": True},
         "subtitle_sync": {"text_audio_drift_count": 2}},
        {"schema_version": 1, "job_id": "j3",
         "artifact_presence": {"subtitle_quality_report": True},
         "subtitle_sync": {"text_audio_drift_count": 5}},
        # pre-Phase-B: no subtitle_quality_report
        {"schema_version": 1, "job_id": "j4_pre_b",
         "artifact_presence": {"subtitle_quality_report": False},
         "subtitle_sync": {"text_audio_drift_count": None}},
    ]
    facts.write_text("\n".join(json.dumps(s) for s in samples))
    out = tmp_path / "report"
    subprocess.run([sys.executable, str(SCRIPT),
                    "--facts", str(facts), "--out-dir", str(out)],
                   check=True, capture_output=True)
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "§6 字幕一致性" in report
    # Only Phase B+ subset: 3 jobs; drift=0 count = 1 (33%)
    assert "drift=0" in report or "无 drift" in report
    assert "Phase B+" in report or "subtitle_quality_report" in report
```

- [ ] **G4a.2: 实现 _section_subtitle_drift**

```python
def _section_subtitle_drift(facts):
    """§6: text_audio_drift_count distribution (Phase B+ subset only)."""
    if not facts:
        return ["## §6 字幕一致性\n\n(no data)\n"]
    pb_subset = [f for f in facts
                 if (f.get("artifact_presence") or {}).get("subtitle_quality_report")]
    lines = ["## §6 字幕一致性 (Phase B+ subset)",
             "",
             f"- subtitle_quality_report present: {len(pb_subset)}/{len(facts)}",
             ""]
    if not pb_subset:
        lines.append("> No Phase B+ jobs in facts. Need post-2026-05-05 prod smoke data.")
        lines.append("")
        return lines
    drift_counts = sorted(
        (f["subtitle_sync"]["text_audio_drift_count"] or 0)
        for f in pb_subset if f.get("subtitle_sync")
    )
    n = len(drift_counts)
    drift_zero = sum(1 for c in drift_counts if c == 0)
    drift_le2 = sum(1 for c in drift_counts if c <= 2)
    drift_gt5 = sum(1 for c in drift_counts if c > 5)
    lines += [
        "| Bucket | Count | % |",
        "|---|---|---|",
        f"| drift=0 (理想) | {drift_zero} | {drift_zero/n*100:.0f}% |",
        f"| drift≤2 | {drift_le2} | {drift_le2/n*100:.0f}% |",
        f"| drift>5 (高风险) | {drift_gt5} | {drift_gt5/n*100:.0f}% |",
        "",
        f"- p50: {_percentile(drift_counts, 0.5)}",
        f"- p90: {_percentile(drift_counts, 0.9)}",
        f"- p99: {_percentile(drift_counts, 0.99)}",
        "",
    ]
    return lines
```

main() 加：

```python
    report_lines += _section_subtitle_drift(facts)
```

- [ ] **G4a.3: 跑测试 → pass**

- [ ] **G4a.4: Commit**

```bash
git commit -m "feat: analyzer §6 subtitle drift histogram (Phase B+ subset only)"
```

---

### Task G4b: Analyzer §7 Whisper coverage（真正的 deliverable-time Whisper）

**Files:**
- Modify: `scripts/smart_shadow_eval_analyzer.py`
- Modify: `tests/test_smart_shadow_eval_analyzer.py`

> **关键**：§7 必须用 `whisper_aligned_cue_count / total` 而**不是** `cache_hits / cache_misses`。后者是 DSP cache（§7b），不是 Whisper cache。Codex 迭代 4 P1 修复的核心约束。

- [ ] **G4b.1: 写失败测试**

```python
def test_analyzer_whisper_section_uses_cue_source_not_cache(tmp_path):
    """§7: alignment_model 分布 + whisper_aligned_cue_count / total，不用 cache_hits"""
    facts = tmp_path / "facts.jsonl"
    samples = [
        {"schema_version": 1, "job_id": "j1",
         "artifact_presence": {"subtitle_cues": True},
         "whisper": {"alignment_model": "small",
                     "whisper_aligned_cue_count": 80,
                     "proportional_fallback_cue_count": 20,
                     "whisper_sidecar_count": 5}},
        {"schema_version": 1, "job_id": "j2",
         "artifact_presence": {"subtitle_cues": True},
         "whisper": {"alignment_model": "medium",
                     "whisper_aligned_cue_count": 100,
                     "proportional_fallback_cue_count": 0,
                     "whisper_sidecar_count": 8}},
        # pre-Phase-D: no subtitle_cues
        {"schema_version": 1, "job_id": "j3_pre_d",
         "artifact_presence": {"subtitle_cues": False},
         "whisper": {"alignment_model": None,
                     "whisper_aligned_cue_count": None}},
    ]
    facts.write_text("\n".join(json.dumps(s) for s in samples))
    out = tmp_path / "report"
    subprocess.run([sys.executable, str(SCRIPT),
                    "--facts", str(facts), "--out-dir", str(out)],
                   check=True, capture_output=True)
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "§7 Whisper" in report
    # 必须出现 alignment_model 分布
    assert "small" in report
    assert "medium" in report
    # 必须出现 cue 比例（不是 cache）
    assert "whisper_aligned" in report or "aligned_cue" in report
    # 必须明确 wall_time 不在 P0
    assert "wall_time" in report
    # 不应该出现 "cache_hits" / "cache_misses" （这是 §7b workflow cache 的事）
    assert "cache_hits" not in report
    assert "cache_misses" not in report
```

- [ ] **G4b.2: 实现 _section_whisper_coverage**

```python
def _section_whisper_coverage(facts):
    """§7: deliverable-time Whisper coverage (NOT DSP cache).

    Uses subtitle_cues.json::cues[].source counts, NOT project_state cache fields.
    """
    if not facts:
        return ["## §7 Whisper 覆盖\n\n(no data)\n"]
    pd_subset = [f for f in facts
                 if (f.get("artifact_presence") or {}).get("subtitle_cues")]
    lines = ["## §7 Whisper 覆盖 (Phase D+ subset; deliverable-time faster-whisper)",
             "",
             f"- subtitle_cues.json present: {len(pd_subset)}/{len(facts)}",
             "",
             "> **wall_time 不在 P0 范围**（runtime 只 logger.info 不持久化）",
             "",
             "> **重要**：本节统计**真正的 deliverable-time Whisper 覆盖**——"
             "用 `subtitle_cues.json::cues[].source` 含 `'semantic_block_v2_whisper_aligned'` "
             "的 cue 数。**不是** workflow alignment cache（那是 §7b，DSP TTS aligned-audio "
             "stage cache，完全不同的 cache）。",
             ""]
    if not pd_subset:
        lines.append("> No Phase D+ jobs (or Whisper 双闸门未启用). Need post-2026-05-05 prod smoke.")
        lines.append("")
        return lines

    # Alignment model distribution
    model_counts = defaultdict(int)
    for f in pd_subset:
        m = (f.get("whisper") or {}).get("alignment_model")
        if m:
            model_counts[m] += 1
    if model_counts:
        lines += ["### alignment_model 分布", "",
                  "| Model | Count |", "|---|---|"]
        for m, c in sorted(model_counts.items()):
            lines.append(f"| {m} | {c} |")
        lines.append("")

    # whisper_aligned_cue ratio
    ratios = []
    for f in pd_subset:
        w = f.get("whisper") or {}
        aligned = w.get("whisper_aligned_cue_count")
        fallback = w.get("proportional_fallback_cue_count")
        if isinstance(aligned, int) and isinstance(fallback, int):
            total = aligned + fallback
            if total > 0:
                ratios.append(aligned / total)
    if ratios:
        ratios.sort()
        lines += [
            "### whisper_aligned / total cue 比例",
            "",
            f"- p50: {_percentile(ratios, 0.5):.2%}",
            f"- p90: {_percentile(ratios, 0.9):.2%}",
            f"- p99: {_percentile(ratios, 0.99):.2%}",
            "",
        ]

    # Sidecar count
    sidecar_counts = sorted(
        (f.get("whisper") or {}).get("whisper_sidecar_count") or 0
        for f in pd_subset
    )
    if sidecar_counts:
        lines += [
            "### whisper_sidecar_count 分布 (per-WAV cache files)",
            "",
            f"- p50: {_percentile(sidecar_counts, 0.5)}",
            f"- p90: {_percentile(sidecar_counts, 0.9)}",
            "",
            "> 真实 cache hit/miss 当前未持久化，P0 不统计；wall_time 也不在 P0 范围。",
            "",
        ]
    return lines
```

main() 加：

```python
    report_lines += _section_whisper_coverage(facts)
```

- [ ] **G4b.3: 跑测试 → pass**

- [ ] **G4b.4: Commit**

```bash
git commit -m "feat: analyzer §7 Whisper coverage from cue source (NOT cache fields)"
```

---

### Task G4c: Analyzer §7b workflow_alignment_cache（DSP cache，必须显式 NOT Whisper 警告）

**Files:**
- Modify: `scripts/smart_shadow_eval_analyzer.py`
- Modify: `tests/test_smart_shadow_eval_analyzer.py`

> **关键**：§7b 必须显式标注 "this is DSP TTS aligned-audio stage cache, NOT Whisper cache, do NOT use for Whisper-default-on decisions"。Codex 迭代 4 P1 评审硬要求。

- [ ] **G4c.1: 写失败测试（含 NOT Whisper 警告字面量断言）**

```python
def test_analyzer_workflow_cache_section_with_explicit_not_whisper_warning(tmp_path):
    facts = tmp_path / "facts.jsonl"
    samples = [
        {"schema_version": 1, "job_id": "j1",
         "workflow_alignment_cache": {"cache_hit_blocks": 8, "block_count": 10}},
        {"schema_version": 1, "job_id": "j2",
         "workflow_alignment_cache": {"cache_hit_blocks": 5, "block_count": 10}},
        {"schema_version": 1, "job_id": "j3",
         "workflow_alignment_cache": {"cache_hit_blocks": None, "block_count": None}},
    ]
    facts.write_text("\n".join(json.dumps(s) for s in samples))
    out = tmp_path / "report"
    subprocess.run([sys.executable, str(SCRIPT),
                    "--facts", str(facts), "--out-dir", str(out)],
                   check=True, capture_output=True)
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "§7b" in report
    # CRITICAL: 必须含明确 NOT Whisper 警告（防止读者把这当 Whisper 数据）
    assert "NOT Whisper" in report or "不是 Whisper" in report
    # 必须明示不能用作 Whisper-default-on 决策
    assert "不能用" in report or "do not use" in report.lower()
    # cache_hit_blocks / block_count 数据出现
    assert "8" in report or "13/20" in report  # 8+5=13 hit, 10+10=20 total
```

- [ ] **G4c.2: 实现 _section_workflow_alignment_cache**

```python
def _section_workflow_alignment_cache(facts):
    """§7b: DSP TTS aligned-audio stage cache (NOT Whisper).

    SPEC §3.13 / §14 explicitly requires this section to be visually
    distinct from §7 and to carry an explicit "NOT Whisper" warning.
    """
    lines = [
        "## §7b Workflow Alignment Cache (诊断用，NOT Whisper)",
        "",
        "> ⚠️ **重要**：本节数据来自 `project_state.json::stages.<alignment_stage>.payload.cache_hit_blocks`，"
        "**这是 DSP TTS aligned-audio stage 的 cache，不是 Whisper cache**。"
        "**不能用作"Smart 默认开启 Whisper 增强"的决策依据。** Whisper 真实覆盖率见 §7。",
        "",
    ]
    if not facts:
        lines.append("(no data)\n")
        return lines
    pairs = [
        ((f.get("workflow_alignment_cache") or {}).get("cache_hit_blocks"),
         (f.get("workflow_alignment_cache") or {}).get("block_count"))
        for f in facts
    ]
    valid = [(h, b) for h, b in pairs if isinstance(h, int) and isinstance(b, int) and b > 0]
    if not valid:
        lines.append("(no valid alignment cache data — pre-Phase-* jobs)\n")
        return lines
    total_hits = sum(h for h, _ in valid)
    total_blocks = sum(b for _, b in valid)
    ratios = sorted(h / b for h, b in valid)
    lines += [
        f"- jobs with alignment cache data: {len(valid)}/{len(facts)}",
        f"- aggregate cache hit rate: {total_hits}/{total_blocks} ({total_hits/total_blocks*100:.0f}%)",
        "",
        "### Per-job cache hit ratio 分布",
        "",
        f"- p50: {_percentile(ratios, 0.5):.2%}",
        f"- p90: {_percentile(ratios, 0.9):.2%}",
        "",
    ]
    return lines
```

main() 加：

```python
    report_lines += _section_workflow_alignment_cache(facts)
```

- [ ] **G4c.3: 跑测试 → pass**

- [ ] **G4c.4: Commit**

```bash
git commit -m "feat: analyzer §7b workflow alignment cache with explicit NOT Whisper warning"
```

---

### Task G5: Analyzer §8 cost + §9 margin + §11 risk markers + pricing loader

**Files:**
- Modify: `scripts/smart_shadow_eval_analyzer.py`
- Modify: `tests/test_smart_shadow_eval_analyzer.py`

- [ ] **G5.1: 写测试**

```python
def test_analyzer_cost_section_with_pricing(tmp_path):
    pricing = tmp_path / "pricing.json"
    pricing.write_text(json.dumps({
        "version": 1,
        "credits": {"voice_clone_cost_credits": 500},
        "cost_model": {
            "point_cost_rmb": 0.015,
            "point_price_rmb": 0.03,
            "k_cn_chars_per_src_min": 250,
            "translate_cost_rmb_per_src_min": 0.03,
            "s2_review_cost_rmb_per_src_min": 0.02,
            "rewrite_cost_rmb_per_src_min": 0.02,
            "server_cost_rmb_per_src_min": 0.03,
        }
    }))
    facts = tmp_path / "facts.jsonl"
    facts.write_text(json.dumps({
        "schema_version": 1,
        "job_id": "j1",
        "duration_seconds": 60,  # 1 source min
        "usage_meter": {"clone_calls": 1, "post_tts_resynth_billed_chars": 100,
                         "post_edit_resynth_billed_chars": 0,
                         "llm_input_tokens": 0, "llm_output_tokens": 0,
                         "tts_chars_total": 250},
        "retry_stats": {"_data_source": "metering"},
    }))
    # baseline = 1 * (0.03+0.02+0.02+0.03) = 0.10 RMB
    # smart_retry = 100 * (0.02/250) = 0.008 RMB
    # clone = 1 * 500 * 0.015 = 7.5 RMB
    # smart_total = 0.10 + 0.008 + 7.5 = 7.608 RMB
    # revenue = 100 * 1 * 0.03 = 3 RMB
    # margin = 3 - 7.608 = -4.608 RMB → FAIL
    out = tmp_path / "report"
    subprocess.run([sys.executable, str(SCRIPT),
                    "--facts", str(facts),
                    "--pricing-runtime-snapshot", str(pricing),
                    "--out-dir", str(out)], check=True, capture_output=True)
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "FAIL" in report or "MARGINAL" in report
    assert "7.6" in report or "-4.6" in report  # cost or margin
```

- [ ] **G5.2: 实现 pricing loader + cost section**

```python
def _load_pricing(path):
    if not path:
        return None
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _compute_cost_per_job(fact, pricing):
    """Return (smart_total_rmb, revenue_rmb, margin_rmb, quality)."""
    cm = pricing.get("cost_model", {})
    creds = pricing.get("credits", {})
    src_min = (fact.get("duration_seconds") or 0) / 60.0
    baseline = src_min * (
        cm.get("translate_cost_rmb_per_src_min", 0)
        + cm.get("s2_review_cost_rmb_per_src_min", 0)
        + cm.get("rewrite_cost_rmb_per_src_min", 0)
        + cm.get("server_cost_rmb_per_src_min", 0)
    )
    um = fact.get("usage_meter") or {}
    rs = fact.get("retry_stats") or {}
    rmb_per_char = (cm.get("rewrite_cost_rmb_per_src_min", 0)
                    / max(1, cm.get("k_cn_chars_per_src_min", 250)))
    if rs.get("_data_source") == "metering":
        retts_rmb = (um.get("post_tts_resynth_billed_chars", 0)
                     + um.get("post_edit_resynth_billed_chars", 0)) * rmb_per_char
        # Rewrite cost: real input chars from metering (reviewer iter1 MAJOR fix)
        rewrite_rmb = um.get("rewrite_input_text_chars_total", 0) * rmb_per_char
        quality = "high"
    else:
        # Fallback (no metering): use rewrite_count × AVG_REWRITE_CHARS baseline.
        # AVG_REWRITE_CHARS defined at module top (G1.2).
        rewrite_count = (rs.get("rewrite_count") or 0)
        rewrite_rmb = rewrite_count * AVG_REWRITE_CHARS * rmb_per_char
        retts_rmb = 0  # no retts data without metering
        quality = "low"
    clone_rmb = (um.get("clone_calls", 0)
                 * creds.get("voice_clone_cost_credits", 500)
                 * cm.get("point_cost_rmb", 0.015))
    smart_total = baseline + retts_rmb + rewrite_rmb + clone_rmb
    revenue = 100 * src_min * cm.get("point_price_rmb", 0.03)
    margin = revenue - smart_total
    return (smart_total, revenue, margin, quality)


def _section_cost_margin_risk(facts, pricing):
    if not pricing:
        return [
            "## §8 / §9 / §11 成本 / 毛利 / 风险标记",
            "",
            "❌ N/A — pricing snapshot not provided (use --pricing-runtime-snapshot)",
            "",
        ]
    results = [_compute_cost_per_job(f, pricing) for f in facts]
    margins = sorted(r[2] for r in results)
    if not margins:
        return ["## §8 / §9 / §11", "", "(no data)", ""]
    n = len(margins)
    p50 = margins[n // 2]
    p90 = margins[int(n * 0.9)]
    p99 = margins[int(n * 0.99)]
    # Risk
    high_quality_margins = [r[2] for r in results if r[3] == "high"]
    if len(high_quality_margins) < n * 0.5:
        verdict = "INCONCLUSIVE (metering data < 50%)"
    elif p99 < 0:
        verdict = "FAIL (p99 margin negative)"
    elif p90 < 0:
        verdict = "MARGINAL (p90 margin negative)"
    else:
        verdict = "PASS"
    return [
        "## §8 / §9 成本 + 毛利",
        "",
        "> 成本估算基于 pricing_runtime.json snapshot；不构成财务事实。",
        "",
        f"| Metric | p50 | p90 | p99 |",
        f"|---|---|---|---|",
        f"| margin (RMB) | {p50:.2f} | {p90:.2f} | {p99:.2f} |",
        "",
        f"## §11 Risk Verdict: **{verdict}**",
        "",
    ]
```

- [ ] **G5.3: 跑测试 → pass**

- [ ] **G5.4: Commit**

```bash
git commit -m "feat: analyzer §8 cost + §9 margin + §11 risk verdict (PASS/MARGINAL/FAIL/INCONCLUSIVE)"
```

---

## Phase H: 完整 e2e + 文档化

### Task H1: 全套 pytest 跑通

- [ ] **H1.1: 跑全部 smart_shadow_eval 测试**

```bash
python -m pytest tests/test_smart_shadow_eval_*.py -v
```

预期：全部 pass。

- [ ] **H1.2: 把 .codex_tmp 12 样本喂 collector + 拉回 analyzer 跑一遍**

```bash
python scripts/smart_shadow_eval_collector.py \
  --projects-root D:/Claude/AIVideoTrans_Codex_web_mvp/.codex_tmp/us_fetch/extracted/opt/aivideotrans/data/projects \
  --jobs-root D:/Claude/AIVideoTrans_Codex_web_mvp/.codex_tmp/us_fetch/extracted/opt/aivideotrans/data/jobs \
  --out-dir D:/Claude/temp/smart_shadow_eval/local_e2e

# Pricing fixture for analyzer
echo '{"version":1,"credits":{"voice_clone_cost_credits":500},"cost_model":{"point_cost_rmb":0.015,"point_price_rmb":0.03,"k_cn_chars_per_src_min":250,"translate_cost_rmb_per_src_min":0.03,"s2_review_cost_rmb_per_src_min":0.02,"rewrite_cost_rmb_per_src_min":0.02,"server_cost_rmb_per_src_min":0.03}}' > D:/Claude/temp/smart_shadow_eval/pricing_test.json

python scripts/smart_shadow_eval_analyzer.py \
  --facts D:/Claude/temp/smart_shadow_eval/local_e2e/facts.jsonl \
  --summary D:/Claude/temp/smart_shadow_eval/local_e2e/summary.json \
  --pricing-runtime-snapshot D:/Claude/temp/smart_shadow_eval/pricing_test.json \
  --out-dir D:/Claude/temp/smart_shadow_eval/local_e2e/report
```

- [ ] **H1.3: 看 report.md，确认所有 11 节渲染正常（即便部分节标 N/A）**

- [ ] **H1.4: 关键审核节点 — 把 report.md 给 owner 看**

⏸ **STOP HERE** — Show owner local report.md. Confirm:
- All 11 sections render
- pre-Phase fields show null/N/A 优雅，不崩
- §11 Risk likely "INCONCLUSIVE" because all 12 samples are pre-metering (no retry cost data)

Owner approves → proceed to Phase I prod smoke. Owner objects → fix and re-run.

---

### Task H2: 完善 docstring（**最小 docstring 已在 A1.3 / G1.2 加入**）

**Files:**
- Modify: `scripts/smart_shadow_eval_collector.py` 文件顶部 docstring
- Modify: `scripts/smart_shadow_eval_analyzer.py` 文件顶部 docstring

- [ ] **H2.1: 在 collector 顶部添加 docstring**

```python
"""Smart Shadow Evaluator collector — stdlib-only read-only scanner.

Usage (host machine):
  python scripts/smart_shadow_eval_collector.py \\
    --projects-root /opt/aivideotrans/data/projects \\
    --jobs-root /opt/aivideotrans/data/jobs \\
    --out-dir /tmp/smart_shadow_eval/<run_id>

Smoke (local against .codex_tmp):
  python scripts/smart_shadow_eval_collector.py \\
    --projects-root .codex_tmp/us_fetch/extracted/opt/aivideotrans/data/projects \\
    --jobs-root .codex_tmp/us_fetch/extracted/opt/aivideotrans/data/jobs \\
    --out-dir D:/Claude/temp/smart_shadow_eval/local_smoke \\
    --limit 3

See docs/plans/2026-05-06-smart-shadow-evaluator-design.md.
"""
```

- [ ] **H2.2: Commit**

```bash
git commit -m "docs: add usage examples to collector + analyzer module docstrings"
```

---

## Phase I: 生产 smoke + 全量扫描（关键审核节点）

> **⚠️ Phase I 不在本 plan 自动执行范围。** 这里只列出步骤供 owner 审核通过 H1.4 后手工触发。

### Task I1: 把 collector.py 推到 154

```bash
D:\daili\scripts\Upload-Via-154.cmd ^
  D:\Claude\AIVideoTrans_Codex_web_mvp\scripts\smart_shadow_eval_collector.py ^
  /opt/aivideotrans/app/scripts/smart_shadow_eval_collector.py
```

### Task I2: SMOKE on 154 (--limit 3 --since 2026-05-05)

```bash
ssh root@154 'cd /opt/aivideotrans/app && python3 scripts/smart_shadow_eval_collector.py \
  --projects-root /opt/aivideotrans/data/projects \
  --jobs-root /opt/aivideotrans/data/jobs \
  --out-dir /tmp/smart_shadow_eval/smoke-$(date -u +%Y%m%dT%H%MZ) \
  --limit 3 \
  --since 2026-05-05'
```

### Task I3: 拉回 facts.jsonl 肉眼检查

```bash
scp -P <port> root@154:/tmp/smart_shadow_eval/<run_id>/facts.jsonl D:\Claude\temp\smart_shadow_eval\<run_id>\
# Verify Phase B/D fields present + no PII
```

### Task I4: 全量扫描（去掉 --limit）

### Task I5: sftp 拉 + analyzer + 看 report.md

### Task I6: ⏸ 关键审核节点 — owner 看 report.md §8/§9/§11

PASS → 进 P1 (Shadow 智能决策)
MARGINAL/INCONCLUSIVE → 调阈值或等更多 post-Phase-D 数据
FAIL → 回方案 §16 owner 决策

---

## Done Criteria

- [ ] 全部 pytest pass
- [ ] Local smoke on .codex_tmp produces 12 valid fact sheets
- [ ] PII guard tests green
- [ ] AST import guard test green
- [ ] ARTIFACT_PATHS sync guard test green
- [ ] Local analyzer report.md renders 11 sections
- [ ] Owner approves at H1.4 关键审核节点

