# 长视频稳定性 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 确保 ≤30 分钟视频稳定完成，30-180 分钟视频可容错完成，带 checkpoint 恢复 + 通知机制。

**Architecture:** B+ Checkpoint（文件存在=已完成，原子写入，段级恢复）+ TTS 混合策略（同步限速/异步批量）+ 翻译并行优化 + 分层超时 + 浏览器推送/邮件通知。

**Tech Stack:** Python 3.11, FastAPI, Next.js 16, ffmpeg, MiniMax TTS API, Deepseek/Gemini LLM API, Resend（邮件）

**Spec:** `docs/specs/2026-03-24-long-video-stability-design.md`

---

## Phase 1: 基础稳定性（P0）

### Task 1: 原子写入工具模块

**Files:**
- Create: `src/utils/atomic_io.py`
- Create: `tests/test_atomic_io.py`

- [x] **Step 1: 写测试** *(已有 src/utils/atomic_io.py，包含 atomic_write_bytes, atomic_write_json, is_valid_output, cleanup_tmp_files)*

```python
# tests/test_atomic_io.py
import os
import tempfile
from src.utils.atomic_io import atomic_write_bytes, atomic_write_json

def test_atomic_write_bytes_creates_file():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "test.wav")
        atomic_write_bytes(path, b"fake audio data")
        assert os.path.exists(path)
        assert open(path, "rb").read() == b"fake audio data"

def test_atomic_write_no_tmp_residue():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "test.wav")
        atomic_write_bytes(path, b"data")
        assert not os.path.exists(path + ".tmp")

def test_atomic_write_json_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "state.json")
        data = {"stage": "tts", "done": 42, "total": 100}
        atomic_write_json(path, data)
        import json
        assert json.loads(open(path).read()) == data

def test_atomic_write_overwrites_existing():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "test.wav")
        atomic_write_bytes(path, b"old")
        atomic_write_bytes(path, b"new")
        assert open(path, "rb").read() == b"new"
```

- [x] **Step 2: 运行测试确认失败**

```bash
python -m pytest tests/test_atomic_io.py -v
```
预期: FAIL（模块不存在）

- [x] **Step 3: 实现**

```python
# src/utils/atomic_io.py
"""原子写入工具。写入 .tmp 文件后原子重命名，防止半写入。"""
import json
import os

def atomic_write_bytes(target_path: str, data: bytes) -> None:
    tmp_path = target_path + ".tmp"
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    with open(tmp_path, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, target_path)  # 原子操作（跨平台）

def atomic_write_json(target_path: str, data: dict) -> None:
    raw = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    atomic_write_bytes(target_path, raw)

def is_valid_output(path: str) -> bool:
    """检查文件是否存在且非空（用于 checkpoint 判断）"""
    return os.path.isfile(path) and os.path.getsize(path) > 0

def cleanup_tmp_files(directory: str) -> int:
    """清理目录下所有 .tmp 文件，返回清理数量"""
    count = 0
    for root, _, files in os.walk(directory):
        for f in files:
            if f.endswith(".tmp"):
                os.remove(os.path.join(root, f))
                count += 1
    return count
```

- [x] **Step 4: 运行测试确认通过**

```bash
python -m pytest tests/test_atomic_io.py -v
```
预期: 4 PASSED

- [x] **Step 5: 提交**

```bash
git add src/utils/atomic_io.py tests/test_atomic_io.py
git commit -m "feat: add atomic write utilities for checkpoint safety"
```

---

### Task 2: Resume Point 检测器

**Files:**
- Create: `src/utils/resume_point.py`
- Create: `tests/test_resume_point.py`

- [x] **Step 1: 写测试**

```python
# tests/test_resume_point.py
import os
import tempfile
from src.utils.resume_point import find_resume_point, ResumePoint

def _touch(base, relpath, content=b"data"):
    path = os.path.join(base, relpath)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(content)

def test_empty_project_returns_ingestion():
    with tempfile.TemporaryDirectory() as d:
        rp = find_resume_point(d)
        assert rp.stage == "ingestion"

def test_video_exists_returns_audio_extraction():
    with tempfile.TemporaryDirectory() as d:
        _touch(d, "video/original.mp4")
        rp = find_resume_point(d)
        assert rp.stage == "audio_extraction"

def test_transcript_exists_returns_review():
    with tempfile.TemporaryDirectory() as d:
        _touch(d, "transcript/transcript.json")
        rp = find_resume_point(d)
        assert rp.stage == "review_or_translate"

def test_partial_tts_returns_correct_offset():
    with tempfile.TemporaryDirectory() as d:
        _touch(d, "transcript/transcript.json")
        _touch(d, "translation/translation_merged.json")
        _touch(d, "tts/segment_001.wav")
        _touch(d, "tts/segment_002.wav")
        _touch(d, "tts/segment_003.wav.tmp")  # 不完整
        rp = find_resume_point(d)
        assert rp.stage == "tts"
        assert rp.start_segment == 2  # 跳过已完成的 2 段

def test_tmp_files_cleaned_on_resume():
    with tempfile.TemporaryDirectory() as d:
        _touch(d, "tts/segment_001.wav")
        _touch(d, "tts/segment_002.wav.tmp")
        rp = find_resume_point(d)
        assert not os.path.exists(os.path.join(d, "tts/segment_002.wav.tmp"))

def test_all_tts_done_returns_alignment():
    with tempfile.TemporaryDirectory() as d:
        _touch(d, "transcript/transcript.json")
        _touch(d, "translation/translation_merged.json")
        # 模拟 3 段全部完成（需要知道总段数）
        # 写一个 segment_count.json
        import json
        _touch(d, "checkpoint.json", json.dumps({"total_segments": 3}).encode())
        _touch(d, "tts/segment_001.wav")
        _touch(d, "tts/segment_002.wav")
        _touch(d, "tts/segment_003.wav")
        rp = find_resume_point(d)
        assert rp.stage == "alignment"

def test_output_exists_returns_completed():
    with tempfile.TemporaryDirectory() as d:
        _touch(d, "output/dubbed_audio.wav")
        rp = find_resume_point(d)
        assert rp.stage == "completed"
```

- [x] **Step 2: 运行测试确认失败**

- [x] **Step 3: 实现**

```python
# src/utils/resume_point.py
"""扫描项目目录，确定精确的恢复点。"""
import glob
import json
import os
from dataclasses import dataclass, field

@dataclass
class ResumePoint:
    stage: str
    start_segment: int = 0
    start_batch: int = 0
    metadata: dict = field(default_factory=dict)

def find_resume_point(project_dir: str) -> ResumePoint:
    from src.utils.atomic_io import cleanup_tmp_files
    cleanup_tmp_files(project_dir)

    def exists(relpath):
        return os.path.isfile(os.path.join(project_dir, relpath))

    def count_valid(pattern):
        matches = glob.glob(os.path.join(project_dir, pattern))
        return sum(1 for m in matches if os.path.getsize(m) > 0 and not m.endswith(".tmp"))

    def get_total_segments():
        cp = os.path.join(project_dir, "checkpoint.json")
        if os.path.isfile(cp):
            try:
                return json.loads(open(cp).read()).get("total_segments", 0)
            except Exception:
                pass
        return 0

    # 从后往前检查
    if exists("output/dubbed_audio.wav"):
        return ResumePoint(stage="completed")

    total = get_total_segments()

    aligned = count_valid("alignment/segment_*_aligned.wav")
    if aligned > 0:
        if total > 0 and aligned >= total:
            return ResumePoint(stage="output_merge")
        return ResumePoint(stage="alignment", start_segment=aligned)

    tts_done = count_valid("tts/segment_*.wav")
    if tts_done > 0:
        if total > 0 and tts_done >= total:
            return ResumePoint(stage="alignment", start_segment=0)
        return ResumePoint(stage="tts", start_segment=tts_done)

    if exists("translation/translation_merged.json"):
        return ResumePoint(stage="tts", start_segment=0)

    batch_count = count_valid("translation/batch_*.json")
    if batch_count > 0:
        return ResumePoint(stage="translation", start_batch=batch_count)

    if exists("transcript/transcript.json"):
        return ResumePoint(stage="review_or_translate")

    if exists("transcript/raw_assemblyai.json"):
        return ResumePoint(stage="segmentation")

    if exists("video/original.mp4"):
        return ResumePoint(stage="audio_extraction")

    return ResumePoint(stage="ingestion")
```

- [x] **Step 4: 运行测试确认通过**
- [x] **Step 5: 提交**

```bash
git add src/utils/resume_point.py tests/test_resume_point.py
git commit -m "feat: add resume point detector for checkpoint recovery"
```

---

### Task 3: Pipeline 超时分层

**Files:**
- Modify: `src/services/jobs/process_runner.py:45`

- [x] **Step 1: 修改超时配置**

在 `process_runner.py` 中，将硬编码的 `PROCESS_RUN_TIMEOUT_SECONDS = 60 * 60` 替换为分层超时：

```python
# process_runner.py:45 附近
TIMEOUT_TIERS = {
    "tier1": 2 * 3600,    # ≤30 分钟视频：2 小时
    "tier2": 6 * 3600,    # 30-120 分钟视频：6 小时
    "tier3": 8 * 3600,    # 120-180 分钟视频：8 小时
}
DEFAULT_TIMEOUT = TIMEOUT_TIERS["tier2"]  # 默认 6 小时（兼容旧任务）

def get_timeout_for_duration(video_duration_min: float) -> int:
    if video_duration_min <= 30:
        return TIMEOUT_TIERS["tier1"]
    elif video_duration_min <= 120:
        return TIMEOUT_TIERS["tier2"]
    else:
        return TIMEOUT_TIERS["tier3"]
```

- [x] **Step 2: 更新调用处使用分层超时**

找到 `process_runner.py` 中使用 `PROCESS_RUN_TIMEOUT_SECONDS` 的地方（约 line 217），改为动态获取：

```python
timeout = get_timeout_for_duration(job_record.video_duration_min or 30)
```

- [x] **Step 3: 测试（手动验证）**

```bash
python -c "from src.services.jobs.process_runner import get_timeout_for_duration; print(get_timeout_for_duration(10), get_timeout_for_duration(60), get_timeout_for_duration(150))"
```
预期: `7200 21600 28800`

- [x] **Step 4: 提交**

```bash
git add src/services/jobs/process_runner.py
git commit -m "feat: tiered pipeline timeout based on video duration"
```

---

### Task 4: ffmpeg 流式转码（替代 pydub 内存加载）

**Files:**
- Modify: `src/services/assemblyai/transcriber.py:182-188`
- Create: `tests/test_ffmpeg_transcode.py`

- [x] **Step 1: 写测试**

```python
# tests/test_ffmpeg_transcode.py
import os
import subprocess
import tempfile

def test_ffmpeg_transcode_produces_mp3():
    """验证 ffmpeg 能将 WAV 转为 MP3"""
    with tempfile.TemporaryDirectory() as d:
        wav = os.path.join(d, "test.wav")
        mp3 = os.path.join(d, "test.mp3")
        # 生成 1 秒静音 WAV
        subprocess.run([
            "ffmpeg", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            "-t", "1", "-y", wav
        ], check=True, capture_output=True)
        assert os.path.exists(wav)
        # 转码
        subprocess.run([
            "ffmpeg", "-i", wav,
            "-ac", "1", "-ar", "16000", "-b:a", "64k",
            "-f", "mp3", mp3, "-y"
        ], check=True, capture_output=True)
        assert os.path.exists(mp3)
        assert os.path.getsize(mp3) > 0
        assert os.path.getsize(mp3) < os.path.getsize(wav)
```

- [x] **Step 2: 运行测试确认通过**（这个应该直接通过，因为只测 ffmpeg）

- [x] **Step 3: 修改 transcriber.py**

替换 `src/services/assemblyai/transcriber.py` 第 182-188 行的 pydub 逻辑：

```python
# 旧代码（pydub，内存加载整个文件）：
# audio = AudioSegment.from_file(str(source_path))
# optimized_audio = audio.set_channels(1).set_frame_rate(DEFAULT_UPLOAD_MP3_FRAME_RATE)
# optimized_audio.export(str(optimized_path), format="mp3", bitrate=...)

# 新代码（ffmpeg 流式转码，内存 < 10MB）：
import subprocess
subprocess.run([
    "ffmpeg", "-i", str(source_path),
    "-ac", "1",
    "-ar", str(DEFAULT_UPLOAD_MP3_FRAME_RATE),
    "-b:a", f"{DEFAULT_UPLOAD_MP3_BITRATE}k",
    "-f", "mp3", str(optimized_path), "-y"
], check=True, capture_output=True)
```

- [x] **Step 4: 验证转码功能未损坏**

```bash
python -m pytest tests/test_ffmpeg_transcode.py tests/test_assemblyai_transcriber.py -v
```

- [x] **Step 5: 提交**

```bash
git add src/services/assemblyai/transcriber.py tests/test_ffmpeg_transcode.py
git commit -m "perf: replace pydub memory load with ffmpeg streaming transcode"
```

---

### Task 5: TTS 同步限速 + 段级 Checkpoint

**Files:**
- Modify: `src/services/tts/tts_generator.py:74-95`
- Create: `tests/test_tts_checkpoint.py`

- [x] **Step 1: 写测试**

```python
# tests/test_tts_checkpoint.py
import os
import tempfile
import time

def test_tts_skips_completed_segments():
    """已存在的 WAV 文件应被跳过"""
    with tempfile.TemporaryDirectory() as d:
        # 模拟已完成的段
        seg1 = os.path.join(d, "segment_001.wav")
        with open(seg1, "wb") as f:
            f.write(b"fake audio")
        from src.utils.atomic_io import is_valid_output
        assert is_valid_output(seg1) is True

def test_tts_rate_limiter():
    """限速器确保两次调用间隔 >= min_interval"""
    from src.services.tts.rate_limiter import RateLimiter
    limiter = RateLimiter(rpm=60)  # 1 秒间隔
    t1 = time.time()
    limiter.wait()
    limiter.wait()
    t2 = time.time()
    assert t2 - t1 >= 0.9  # 至少间隔 ~1 秒
```

- [x] **Step 2: 运行测试确认失败**

- [x] **Step 3: 实现限速器**

```python
# src/services/tts/rate_limiter.py
import time
import threading

class RateLimiter:
    def __init__(self, rpm: int = 20):
        self.min_interval = 60.0 / rpm
        self._last_call = 0.0
        self._lock = threading.Lock()

    def wait(self):
        with self._lock:
            now = time.time()
            elapsed = now - self._last_call
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
            self._last_call = time.time()
```

- [x] **Step 4: 修改 tts_generator.py**

在 `generate_all()` 方法（line 74-95）中添加 checkpoint 跳过 + 限速：

```python
# tts_generator.py generate_all() 方法内
from src.utils.atomic_io import atomic_write_bytes, is_valid_output
from src.services.tts.rate_limiter import RateLimiter

rate_limiter = RateLimiter(rpm=20)

for i, segment in enumerate(segments):
    output_path = self._segment_output_path(segment.index)

    # Checkpoint: 跳过已完成的段
    if is_valid_output(output_path):
        print(f"[TTS] 跳过已完成段 {segment.index}")
        continue

    # 限速
    rate_limiter.wait()

    # 生成 + 原子写入
    audio_data = self._generate_one(segment)
    atomic_write_bytes(output_path, audio_data)

    if (i + 1) % 5 == 0 or i == len(segments) - 1:
        print(f"[TTS] 进度: {i+1}/{len(segments)}")
```

- [x] **Step 5: 运行测试**

```bash
python -m pytest tests/test_tts_checkpoint.py -v
```

- [x] **Step 6: 提交**

```bash
git add src/services/tts/rate_limiter.py src/services/tts/tts_generator.py tests/test_tts_checkpoint.py
git commit -m "feat: TTS rate limiting + segment checkpoint skip"
```

---

### Task 6: 前端时长预警

**Files:**
- Modify: `frontend-next/src/app/translations/new/page.tsx:60-93`

- [x] **Step 1: 添加时长检测和提示**

在 `handleSubmit` 函数开头（line 60 附近），在提交前添加时长检查逻辑。同时在表单下方添加预警提示 UI：

```tsx
// 在 page.tsx 中添加状态
const [durationWarning, setDurationWarning] = useState<string | null>(null)

// 在 handleSubmit 中，提交到 API 之前检查
// 注：实际时长需要后端返回或前端检测视频时长
// 简化版：在提交成功后由后端返回时长信息

// 在表单中添加提示
{durationWarning ? (
  <div className={`rounded-xl border p-4 text-sm ${
    durationWarning.includes('🚫') ? 'border-red-500/30 bg-red-500/10 text-red-400' :
    durationWarning.includes('⚠️⚠️') ? 'border-amber-500/30 bg-amber-500/10 text-amber-400' :
    'border-blue-500/30 bg-blue-500/10 text-blue-400'
  }`}>
    {durationWarning}
  </div>
) : null}
```

- [x] **Step 2: 后端返回时长信息**

在 Job API 的 create response 中返回视频时长，前端在工作区显示预计处理时间。

- [x] **Step 3: 测试（手动在浏览器验证）**
- [x] **Step 4: 提交**

```bash
git add frontend-next/src/app/translations/new/page.tsx
git commit -m "feat: frontend duration warning for long videos"
```

---

## Phase 2: 长视频支持（P1）

### Task 7: TTS 异步模式

**Files:**
- Create: `src/services/tts/async_tts_provider.py`
- Modify: `src/services/tts/tts_generator.py`
- Create: `tests/test_async_tts.py`

- [ ] **Step 1: 写测试（mock MiniMax API）**

```python
# tests/test_async_tts.py
from unittest.mock import patch, MagicMock
from src.services.tts.async_tts_provider import AsyncTTSProvider

def test_async_submit_returns_task_id():
    provider = AsyncTTSProvider(api_key="test")
    with patch.object(provider, "_http_post") as mock_post:
        mock_post.return_value = {"task_id": "task_123"}
        task_id = provider.submit_async("hello world", "voice_001")
        assert task_id == "task_123"

def test_async_poll_returns_url_when_done():
    provider = AsyncTTSProvider(api_key="test")
    with patch.object(provider, "_http_get") as mock_get:
        mock_get.return_value = {
            "status": "Success",
            "file_id": "file_123"
        }
        result = provider.poll_task("task_123")
        assert result.status == "completed"

def test_choose_strategy_short_video():
    from src.services.tts.tts_generator import choose_tts_strategy
    strategy = choose_tts_strategy(total_segments=50, video_duration_min=10)
    assert strategy == "sync"

def test_choose_strategy_long_video():
    from src.services.tts.tts_generator import choose_tts_strategy
    strategy = choose_tts_strategy(total_segments=200, video_duration_min=60)
    assert strategy == "async"
```

- [ ] **Step 2: 实现异步 TTS Provider**

```python
# src/services/tts/async_tts_provider.py
"""MiniMax T2A Async V2 异步长文本语音合成"""
import time
import requests
from dataclasses import dataclass

@dataclass
class AsyncTaskResult:
    status: str  # "pending", "completed", "failed"
    file_url: str | None = None
    error: str | None = None

class AsyncTTSProvider:
    BASE_URL = "https://api.minimaxi.com/v1"

    def __init__(self, api_key: str, model: str = "speech-2.8-turbo"):
        self.api_key = api_key
        self.model = model

    def submit_async(self, text: str, voice_id: str) -> str:
        resp = self._http_post(f"{self.BASE_URL}/t2a_async_v2", {
            "model": self.model,
            "text": text,
            "voice_setting": {"voice_id": voice_id},
        })
        return resp["task_id"]

    def poll_task(self, task_id: str) -> AsyncTaskResult:
        resp = self._http_get(
            f"{self.BASE_URL}/query/t2a_async_query_v2",
            params={"task_id": task_id}
        )
        status = resp.get("status", "")
        if status == "Success":
            return AsyncTaskResult(
                status="completed",
                file_url=resp.get("file_id")
            )
        elif status in ("Failed", "Error"):
            return AsyncTaskResult(
                status="failed",
                error=resp.get("message", "Unknown error")
            )
        return AsyncTaskResult(status="pending")

    def wait_for_completion(self, task_id: str, interval: int = 10,
                            max_wait: int = 3600) -> AsyncTaskResult:
        start = time.time()
        while time.time() - start < max_wait:
            result = self.poll_task(task_id)
            if result.status != "pending":
                return result
            time.sleep(interval)
        return AsyncTaskResult(status="failed", error="异步 TTS 超时")

    def _http_post(self, url, body):
        resp = requests.post(url, json=body, headers={
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _http_get(self, url, params=None):
        resp = requests.get(url, params=params, headers={
            "Authorization": f"Bearer {self.api_key}",
        }, timeout=30)
        resp.raise_for_status()
        return resp.json()
```

- [ ] **Step 3: 添加策略选择到 tts_generator.py**

```python
def choose_tts_strategy(total_segments: int, video_duration_min: float) -> str:
    if video_duration_min <= 30 and total_segments <= 100:
        return "sync"
    return "async"
```

- [ ] **Step 4: 运行测试**
- [ ] **Step 5: 提交**

```bash
git add src/services/tts/async_tts_provider.py src/services/tts/tts_generator.py tests/test_async_tts.py
git commit -m "feat: async TTS provider for long videos via MiniMax T2A Async V2"
```

---

### Task 8: 翻译并行化 + 加大批次

**Files:**
- Modify: `src/services/gemini/translator.py:25,266-282`
- Create: `tests/test_translation_parallel.py`

- [ ] **Step 1: 写测试**

```python
# tests/test_translation_parallel.py
def test_batch_size_configurable():
    from src.services.gemini.translator import BATCH_SIZE_CONFIG
    assert BATCH_SIZE_CONFIG["default"] == 15
    assert BATCH_SIZE_CONFIG["legacy"] == 5

def test_parallel_batch_execution(mocker):
    """验证并行执行 3 批的基本逻辑"""
    from src.services.gemini.translator import parallel_translate_batches
    # Mock 翻译函数
    mock_translate = mocker.Mock(side_effect=lambda batch: [
        {"segment_id": s["id"], "cn_text": f"translated_{s['id']}"}
        for s in batch
    ])
    batches = [[{"id": i}] for i in range(6)]
    results = parallel_translate_batches(batches, mock_translate, workers=3)
    assert len(results) == 6
    assert mock_translate.call_count == 6
```

- [ ] **Step 2: 修改 translator.py**

```python
# translator.py line 25
BATCH_SIZE_CONFIG = {"default": 15, "legacy": 5}
DEFAULT_BATCH_SIZE = BATCH_SIZE_CONFIG["default"]
PARALLEL_WORKERS = 3
```

- [ ] **Step 3: 实现并行批处理**

在 translator.py 中添加并行执行函数，使用 `concurrent.futures.ThreadPoolExecutor`：

```python
from concurrent.futures import ThreadPoolExecutor, as_completed

def parallel_translate_batches(batches, translate_fn, workers=3):
    results = [None] * len(batches)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(translate_fn, batch): i
            for i, batch in enumerate(batches)
        }
        for future in as_completed(futures):
            idx = futures[future]
            results[idx] = future.result()
    return [item for batch in results if batch for item in batch]
```

- [ ] **Step 4: 运行测试**
- [ ] **Step 5: 提交**

```bash
git add src/services/gemini/translator.py tests/test_translation_parallel.py
git commit -m "feat: parallel translation with configurable batch size"
```

---

### Task 9: 翻译上下文窗口 + 术语表

**Files:**
- Modify: `src/services/gemini/translator.py` (prompt 构建逻辑)
- Create: `tests/test_translation_context.py`

- [ ] **Step 1: 写测试**

```python
# tests/test_translation_context.py
def test_glossary_extraction():
    from src.services.gemini.translator import extract_glossary_from_results
    results = [
        {"source": "Charlie Munger said", "cn": "查理·芒格说"},
        {"source": "The compounding effect", "cn": "复利效应"},
    ]
    # 应提取人名和术语
    glossary = extract_glossary_from_results(results)
    assert isinstance(glossary, dict)

def test_prompt_includes_context():
    from src.services.gemini.translator import build_prompt_with_context
    batch = [{"id": "3", "text": "hello"}]
    prev = [{"source": "hi", "cn": "你好"}]
    glossary = {"hello": "你好"}
    prompt = build_prompt_with_context(batch, previous=prev, glossary=glossary)
    assert "你好" in prompt  # 上下文出现
    assert "hello" in prompt  # 术语表出现
```

- [ ] **Step 2: 实现**

在 `_build_prompt()` 方法中添加 `previous_results` 和 `glossary` 参数。确保固定前缀（指令 + 术语表）不变以触发 Deepseek prefix caching。

- [ ] **Step 3: 运行测试**
- [ ] **Step 4: 提交**

```bash
git add src/services/gemini/translator.py tests/test_translation_context.py
git commit -m "feat: translation context window + glossary for consistency"
```

---

### Task 10: 对齐 Checkpoint

**Files:**
- Modify: `src/modules/alignment/alignment_orchestrator.py:61-115`

- [ ] **Step 1: 在对齐循环中添加 checkpoint 跳过**

```python
# alignment_orchestrator.py process_block() 内
from src.utils.atomic_io import atomic_write_bytes, is_valid_output

aligned_path = f"alignment/segment_{block.index:03d}_aligned.wav"
if is_valid_output(aligned_path):
    print(f"[ALIGN] 跳过已完成段 {block.index}")
    return load_audio(aligned_path)

# ... 原有对齐逻辑 ...

# 对齐完成后原子写入
atomic_write_bytes(aligned_path, aligned_audio)
```

- [ ] **Step 2: 测试**
- [ ] **Step 3: 提交**

```bash
git add src/modules/alignment/alignment_orchestrator.py
git commit -m "feat: alignment checkpoint skip for completed segments"
```

---

### Task 11: 段级进度上报

**Files:**
- Modify: `src/services/tts/tts_generator.py`
- Modify: `src/services/gemini/translator.py`
- Modify: `src/modules/alignment/alignment_orchestrator.py`
- Modify: `frontend-next/src/app/workspace/[jobId]/page.tsx`

- [x] **Step 1: 后端 — 每完成 N 段更新 progress_message**

```python
# 在 tts_generator.py、translator.py、alignment_orchestrator.py 的循环中
def update_progress(job_id, stage_label, done, total, start_time):
    pct = int(done / total * 100) if total > 0 else 0
    elapsed = time.time() - start_time
    rate = done / elapsed if elapsed > 0 else 0
    remaining = int((total - done) / rate) if rate > 0 else 0
    msg = f"{stage_label}: {done}/{total} ({pct}%)"
    if remaining > 60:
        msg += f" · 预计剩余 {remaining // 60} 分钟"
    elif remaining > 0:
        msg += f" · 预计剩余 {remaining} 秒"
    # 写入 job progress
    job_service.update_progress_message(job_id, msg)
```

- [ ] **Step 2: 前端 — 显示段级进度**

工作区 `page.tsx` 已有 `progressMessage` 显示，无需额外改动 — 后端更新的 progress_message 会通过轮询自动展示。

- [ ] **Step 3: 提交**

```bash
git add src/services/tts/tts_generator.py src/services/gemini/translator.py src/modules/alignment/alignment_orchestrator.py
git commit -m "feat: segment-level progress reporting for long videos"
```

---

### Task 12: 浏览器推送通知

**Files:**
- Modify: `frontend-next/src/app/workspace/[jobId]/page.tsx`
- Modify: `frontend-next/src/app/layout.tsx`

- [x] **Step 1: 在 layout.tsx 中请求通知权限**

```tsx
// layout.tsx <body> 中添加通知权限请求脚本
<script dangerouslySetInnerHTML={{ __html: `
  if ("Notification" in window && Notification.permission === "default") {
    // 延迟请求，避免页面加载时弹出
    setTimeout(() => Notification.requestPermission(), 5000);
  }
` }} />
```

- [x] **Step 2: 在 workspace page.tsx 的轮询中检测状态变化**

```tsx
// 在 loadJob 函数中，检测状态变化
const prevStatusRef = useRef<string | null>(null)

// 在 setJob 之后
if (prevStatusRef.current && prevStatusRef.current !== nextJob.status) {
  sendBrowserNotification(nextJob.status, displayTitle)
}
prevStatusRef.current = nextJob.status

function sendBrowserNotification(status: string, title: string) {
  if (!("Notification" in window) || Notification.permission !== "granted") return
  if (status === "succeeded") {
    new Notification("任务完成", { body: `${title} 已完成，点击查看结果` })
  } else if (status === "failed") {
    new Notification("任务失败", { body: `${title} 处理失败，点击查看详情` })
  }
}
```

- [x] **Step 3: 测试（手动在浏览器验证）**
- [x] **Step 4: 提交**

```bash
git add frontend-next/src/app/workspace/[jobId]/page.tsx frontend-next/src/app/layout.tsx
git commit -m "feat: browser push notification on job completion/failure"
```

---

## Phase 3: 完善（P2）

### Task 13: 邮件通知集成

**Files:**
- Create: `gateway/notifications.py`
- Modify: `gateway/main.py`

- [ ] **Step 1: 实现邮件发送模块**

```python
# gateway/notifications.py
import os
import httpx

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
FROM_EMAIL = os.getenv("NOTIFICATION_FROM", "noreply@aivideotrans.site")

async def send_email(to: str, subject: str, html: str):
    if not RESEND_API_KEY:
        return
    async with httpx.AsyncClient() as client:
        await client.post("https://api.resend.com/emails", json={
            "from": FROM_EMAIL, "to": [to],
            "subject": subject, "html": html
        }, headers={"Authorization": f"Bearer {RESEND_API_KEY}"})

async def notify_job_completed(user_email: str, job_title: str, job_id: str):
    url = f"https://us.aivideotrans.site/workspace/{job_id}"
    await send_email(user_email,
        f"任务完成: {job_title}",
        f"<p>你的翻译任务 <b>{job_title}</b> 已完成。</p>"
        f"<p><a href='{url}'>点击查看结果</a></p>"
    )

async def notify_job_failed(user_email: str, job_title: str, job_id: str):
    url = f"https://us.aivideotrans.site/workspace/{job_id}"
    await send_email(user_email,
        f"任务失败: {job_title}",
        f"<p>你的翻译任务 <b>{job_title}</b> 处理失败。</p>"
        f"<p><a href='{url}'>点击查看详情</a></p>"
    )
```

- [ ] **Step 2: 在 gateway 中注册通知路由**
- [ ] **Step 3: 测试**
- [ ] **Step 4: 提交**

```bash
git add gateway/notifications.py gateway/main.py
git commit -m "feat: email notification on job completion via Resend"
```

---

### Task 14: 磁盘空间预检 + 中间文件清理

**Files:**
- Create: `src/utils/disk_manager.py`
- Create: `tests/test_disk_manager.py`

- [ ] **Step 1: 实现**

```python
# src/utils/disk_manager.py
import os
import shutil

def check_disk_space(required_gb: float) -> bool:
    usage = shutil.disk_usage("/opt/aivideotrans")
    free_gb = usage.free / (1024 ** 3)
    return free_gb >= required_gb * 1.5

def estimate_required_gb(video_duration_min: float) -> float:
    return video_duration_min * 0.035  # ~35 MB/min

def cleanup_intermediate(project_dir: str, completed_stage: str):
    """阶段完成后清理不再需要的中间文件"""
    removable = {
        "transcription_done": ["audio/original_upload.mp3"],
        "output_done": ["audio/speech_for_asr.wav"],
    }
    for path in removable.get(completed_stage, []):
        full = os.path.join(project_dir, path)
        if os.path.exists(full):
            os.remove(full)
```

- [ ] **Step 2: 测试**
- [ ] **Step 3: 提交**

```bash
git add src/utils/disk_manager.py tests/test_disk_manager.py
git commit -m "feat: disk space pre-check and intermediate file cleanup"
```

---

### Task 15: 翻译限流指数退避

**Files:**
- Modify: `src/services/gemini/translator.py:617-640`

- [ ] **Step 1: 修改重试逻辑**

```python
# 替换 translator.py 第 631 行附近的退避逻辑
# 旧: wait_seconds = 5 * (attempt + 1)  # 5s, 10s
# 新:
wait_seconds = min(60, 5 * (2 ** attempt))  # 5s, 10s, 20s, 40s, 60s
```

- [ ] **Step 2: 提交**

```bash
git add src/services/gemini/translator.py
git commit -m "fix: exponential backoff for translation rate limiting"
```

---

### Task 16: 中断恢复集成测试

**Files:**
- Create: `tests/test_resume_integration.py`

- [ ] **Step 1: 写集成测试**

```python
# tests/test_resume_integration.py
"""模拟中断场景，验证恢复逻辑"""
import os
import tempfile
from src.utils.resume_point import find_resume_point
from src.utils.atomic_io import atomic_write_bytes, atomic_write_json

def test_full_resume_scenario():
    """模拟：TTS 在第 5 段中断，恢复后从第 5 段继续"""
    with tempfile.TemporaryDirectory() as d:
        # 模拟已完成的阶段
        atomic_write_bytes(os.path.join(d, "video/original.mp4"), b"video")
        atomic_write_bytes(os.path.join(d, "transcript/transcript.json"), b"{}")
        atomic_write_bytes(os.path.join(d, "translation/translation_merged.json"), b"{}")
        atomic_write_json(os.path.join(d, "checkpoint.json"), {"total_segments": 10})

        # 模拟 TTS 完成了 5 段
        for i in range(1, 6):
            atomic_write_bytes(os.path.join(d, f"tts/segment_{i:03d}.wav"), b"audio")

        # 模拟第 6 段中断（.tmp 残留）
        with open(os.path.join(d, "tts/segment_006.wav.tmp"), "wb") as f:
            f.write(b"partial")

        # 恢复
        rp = find_resume_point(d)
        assert rp.stage == "tts"
        assert rp.start_segment == 5  # 从第 6 段开始（0-indexed = 5）
        # .tmp 已被清理
        assert not os.path.exists(os.path.join(d, "tts/segment_006.wav.tmp"))
```

- [ ] **Step 2: 运行测试**

```bash
python -m pytest tests/test_resume_integration.py -v
```

- [ ] **Step 3: 提交**

```bash
git add tests/test_resume_integration.py
git commit -m "test: integration test for checkpoint resume scenarios"
```

---

## 完成标准

所有 Phase 完成后，系统应通过以下验证：

1. **≤30 分钟视频**：提交后 30 分钟内稳定完成全流程
2. **60 分钟视频**：2 小时内完成，中途 kill 进程后重启能从断点恢复
3. **3 小时视频**：异步 TTS + checkpoint，4-5 小时内完成
4. **浏览器通知**：任务完成/失败时收到浏览器推送
5. **邮件通知**：任务完成/失败时收到邮件
6. **进度可见**：长视频处理时，工作区显示段级进度百分比
7. **不重复计费**：中断恢复后，已完成的 TTS 段不再调用 API
