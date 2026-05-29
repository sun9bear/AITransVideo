# MiMo 免费版 Phase 1（内部 spike）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把"用真实任务的干净原声参考 + 中文译文调 MiMo voiceclone 生成保留原声的中文配音"从一次性 spike 脚本，固化为**可复用的 provider 能力 + 参考提取步骤 + 可量度的内部 spike 工具**，用于跨境延迟/失败率/质量的批量验证。

**Architecture:** 三块独立单元：(1) `mimo_tts_provider` 新增 voiceclone 合成函数（内联 base64 参考 + 10MB 校验）；(2) 新模块从 `speech_for_asr.wav` 按说话人切 3–5s 干净参考并持久化；(3) `scripts/spike/` 下的测量 harness 串起 (1)(2) 跑批、记录延迟/失败/usage。**不接 gateway service_mode、不动 pipeline/quota/pricing/下载 gate**（那是 Phase 2）。

**Tech Stack:** Python 3.11、stdlib `urllib`/`base64`/`subprocess`(ffmpeg)、pytest（mock HTTP，不打真实 API）。沿用现有 `mimo_tts_provider._post_json` + `_extract_speaker_audio_clips` 的 ffmpeg 切片模式。

**约束（design 文档 §1.5/§5）：** 付费 API 合规——voiceclone 只在开发者/allowlist 显式运行 harness 时调用（现免费）；**绝不**进 CI/自动路径。Smart TTS 不动。consent/法律是**上线 gate**，Phase 1 仅内部样本、不公开。**不创建 worktree/分支，直接在 main 工作**（项目 CLAUDE.md）。

---

## File Structure

| 文件 | 责任 | 操作 |
|---|---|---|
| `src/services/tts/mimo_tts_provider.py` | 加 `synthesize_voiceclone()` + 抽出共享的音频提取 helper | Modify |
| `src/services/tts/voiceclone_reference.py` | 从 clean 人声按说话人切 3–5s 参考、持久化 | Create |
| `scripts/spike/mimo_voiceclone_spike.py` | 内部测量 harness（跑批 + 记录 latency/失败/usage） | Create |
| `tests/test_mimo_voiceclone_provider.py` | provider voiceclone 单测（mock HTTP） | Create |
| `tests/test_voiceclone_reference.py` | 参考切片选段/封顶逻辑单测 | Create |
| `tests/test_mimo_voiceclone_spike.py` | harness smoke（mock provider，不打真实 API） | Create |

---

## Task 1：MiMo voiceclone provider 函数

**Files:**
- Modify: `src/services/tts/mimo_tts_provider.py`
- Test: `tests/test_mimo_voiceclone_provider.py`

- [x] **Step 1: 写失败测试**

```python
# tests/test_mimo_voiceclone_provider.py
import base64
import pytest
import services.tts.mimo_tts_provider as mp


def _ok_response(audio_bytes=b"RIFF....fakewav-payload-over-44-bytes-xxxxxxxxxxxxxxxxxxxx"):
    return {"choices": [{"message": {"audio": {"data": base64.b64encode(audio_bytes).decode()}}}]}


def test_synthesize_voiceclone_returns_audio_bytes(monkeypatch):
    captured = {}
    def fake_post(*, endpoint, api_key, payload, **kw):
        captured["payload"] = payload
        return _ok_response()
    monkeypatch.setattr(mp, "_post_json", fake_post)
    out = mp.synthesize_voiceclone("你好世界这是测试", reference_audio=b"\x00" * 2000, api_key="k")
    assert isinstance(out, bytes) and len(out) >= 44
    # voiceclone model + inline base64 data URI in audio.voice
    p = captured["payload"]
    assert p["model"] == "mimo-v2.5-tts-voiceclone"
    assert p["audio"]["voice"].startswith("data:audio/wav;base64,")
    assert p["messages"][0]["role"] == "assistant"
    assert p["messages"][0]["content"] == "你好世界这是测试"
    assert p["modalities"] == ["audio"]


def test_synthesize_voiceclone_rejects_oversized_reference(monkeypatch):
    monkeypatch.setattr(mp, "_post_json", lambda **kw: _ok_response())
    # >10MB after base64: ~7.5MB raw -> ~10MB b64
    with pytest.raises(mp.MiMoTTSError, match="10MB|exceeds"):
        mp.synthesize_voiceclone("x", reference_audio=b"\x00" * (8 * 1024 * 1024), api_key="k")


def test_synthesize_voiceclone_requires_key(monkeypatch):
    monkeypatch.delenv("MIMO_API_KEY", raising=False)
    with pytest.raises(mp.MiMoTTSError):
        mp.synthesize_voiceclone("x", reference_audio=b"\x00" * 100, api_key=None)
```

- [x] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_mimo_voiceclone_provider.py -q`
Expected: FAIL（`synthesize_voiceclone` 未定义）

- [x] **Step 3: 最小实现**

在 `mimo_tts_provider.py`：先把 `synthesize()` 里提取音频的那段抽成共享 helper（DRY），再加 voiceclone 函数。

```python
# 模块常量区（DEFAULT_MIMO_MODEL 附近）新增：
DEFAULT_MIMO_VOICECLONE_MODEL = "mimo-v2.5-tts-voiceclone"
# MiMo 官方：参考音频 base64 后不超过 10MB
MAX_REFERENCE_B64_LEN = 10 * 1024 * 1024


def _extract_audio_bytes(response_data: dict) -> bytes:
    """从 chat/completions 响应取 base64 音频并解码。供 synthesize / voiceclone 共用。"""
    choices = response_data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise MiMoTTSError("MiMo TTS response missing choices array.")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise MiMoTTSError("MiMo TTS response missing choices[0].message.")
    audio_obj = message.get("audio")
    if not isinstance(audio_obj, dict):
        raise MiMoTTSError("MiMo TTS response missing choices[0].message.audio.")
    audio_b64 = audio_obj.get("data")
    if not audio_b64 or not isinstance(audio_b64, str):
        raise MiMoTTSError("MiMo TTS response missing audio.data (base64).")
    try:
        audio_bytes = base64.b64decode(audio_b64)
    except Exception as exc:
        raise MiMoTTSError("MiMo TTS audio payload is not valid base64.") from exc
    if len(audio_bytes) < 44:
        raise MiMoTTSError(f"MiMo TTS returned suspiciously small audio ({len(audio_bytes)} bytes).")
    return audio_bytes


def synthesize_voiceclone(
    text: str,
    *,
    reference_audio,                       # bytes | str | Path
    api_key: str | None = None,
    endpoint: str = DEFAULT_MIMO_BASE_URL,
    model: str = DEFAULT_MIMO_VOICECLONE_MODEL,
    reference_mime: str = "audio/wav",
    audio_format: str = DEFAULT_MIMO_AUDIO_FORMAT,
    timeout_seconds: float = DEFAULT_MIMO_TIMEOUT_SECONDS,
    max_retries: int = DEFAULT_MIMO_MAX_RETRIES,
    retry_backoff_seconds: float = DEFAULT_MIMO_RETRY_BACKOFF_SECONDS,
) -> bytes:
    """Zero-shot 克隆参考音频音色合成 *text*（plan 2026-05-29 Phase 1）。

    reference_audio: 原说话人干净参考音频（bytes 或文件路径），内联进
    ``audio.voice`` 的 base64 data URI。返回合成音频 bytes。
    """
    if not text or not text.strip():
        raise MiMoTTSError("Text to synthesize is empty.")
    resolved_key = api_key or os.environ.get("MIMO_API_KEY")
    if not resolved_key:
        raise MiMoTTSError("MiMo API key required. Set MIMO_API_KEY or pass api_key.")
    if isinstance(reference_audio, (str, Path)):
        ref_bytes = Path(reference_audio).read_bytes()
    else:
        ref_bytes = bytes(reference_audio)
    b64 = base64.b64encode(ref_bytes).decode("ascii")
    if len(b64) > MAX_REFERENCE_B64_LEN:
        raise MiMoTTSError(
            f"reference audio base64 len {len(b64)} exceeds MiMo 10MB limit; use a shorter clip"
        )
    payload = {
        "model": model,
        "messages": [{"role": "assistant", "content": text}],
        "modalities": ["audio"],
        "audio": {"voice": f"data:{reference_mime};base64,{b64}", "format": audio_format},
    }
    response_data = _post_json(
        endpoint=endpoint, api_key=resolved_key, payload=payload,
        timeout_seconds=timeout_seconds, max_retries=max_retries,
        retry_backoff_seconds=retry_backoff_seconds,
    )
    return _extract_audio_bytes(response_data)
```

同时把 `synthesize()` 末尾的提取段替换为 `return _extract_audio_bytes(response_data)`（DRY，行为不变）。

- [x] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_mimo_voiceclone_provider.py -q`
Expected: PASS（3 条）

- [x] **Step 5: 回归基础 TTS 没被 DRY 重构搞坏**

Run: `python -m pytest tests/test_mimo_tts_v25.py -q`
Expected: PASS

- [x] **Step 6: Commit**

```bash
git add src/services/tts/mimo_tts_provider.py tests/test_mimo_voiceclone_provider.py
git commit -m "feat(mimo): add synthesize_voiceclone (inline ref + 10MB guard) [free-tier Phase 1]"
```

---

## Task 2：参考片段提取（从 clean 人声切 3–5s/说话人，持久化）

**Files:**
- Create: `src/services/tts/voiceclone_reference.py`
- Test: `tests/test_voiceclone_reference.py`

**模式参考：** `src/services/transcript_reviewer.py::_extract_speaker_audio_clips`（最长发言段 + ffmpeg 切片）。本任务区别：源用 **clean** `speech_for_asr.wav`、目标 **3–5s**、输出 **wav 24kHz**、持久化到 job 产物目录（非 `.review_tmp`）。

- [x] **Step 1: 写失败测试**（选段逻辑可纯函数化，避免依赖真实 ffmpeg）

```python
# tests/test_voiceclone_reference.py
from services.tts.voiceclone_reference import pick_reference_window


def test_pick_reference_window_uses_longest_then_caps_to_max():
    # speaker_a: 段 0-2s, 5-12s(7s最长); 期望取最长并封顶到 max(5s)
    segs = [
        {"speaker_id": "speaker_a", "start_ms": 0, "end_ms": 2000},
        {"speaker_id": "speaker_a", "start_ms": 5000, "end_ms": 12000},
    ]
    win = pick_reference_window(segs, "speaker_a", min_s=3.0, max_s=5.0)
    assert win is not None
    start_ms, end_ms = win
    assert start_ms == 5000
    assert (end_ms - start_ms) == 5000  # capped to max_s


def test_pick_reference_window_returns_none_if_all_too_short():
    segs = [{"speaker_id": "s1", "start_ms": 0, "end_ms": 1500}]
    # 最长 1.5s < min 3s 且无法拼接 -> None（调用方应跳过该说话人）
    assert pick_reference_window(segs, "s1", min_s=3.0, max_s=5.0) is None
```

- [x] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_voiceclone_reference.py -q`
Expected: FAIL（模块/函数未定义）

- [x] **Step 3: 最小实现**

```python
# src/services/tts/voiceclone_reference.py
"""从 demucs clean 人声（speech_for_asr.wav）按说话人切短参考片段，供 MiMo
voiceclone 内联使用（plan 2026-05-29 免费版 Phase 1）。"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

REF_DIR_NAME = "voiceclone_ref"   # job_dir/audio/voiceclone_ref/{speaker}.wav


def pick_reference_window(
    segments: list[dict], speaker_id: str, *, min_s: float = 3.0, max_s: float = 5.0
) -> tuple[int, int] | None:
    """选该说话人的参考窗口 (start_ms, end_ms)。取最长发言段，>max_s 则封顶到
    max_s；最长仍 < min_s 则返回 None（调用方跳过该说话人）。"""
    spans = [
        (int(s["start_ms"]), int(s["end_ms"]))
        for s in segments
        if s.get("speaker_id") == speaker_id and s.get("end_ms", 0) > s.get("start_ms", 0)
    ]
    if not spans:
        return None
    start_ms, end_ms = max(spans, key=lambda u: u[1] - u[0])
    if (end_ms - start_ms) / 1000.0 < min_s:
        return None
    if (end_ms - start_ms) / 1000.0 > max_s:
        end_ms = start_ms + int(max_s * 1000)
    return start_ms, end_ms


def extract_speaker_references(
    segments: list[dict],
    speech_audio_path: str | Path,
    out_dir: str | Path,
    *,
    min_s: float = 3.0,
    max_s: float = 5.0,
    sample_rate: int = 24000,
) -> dict[str, Path]:
    """对每个说话人从 *speech_audio_path*（clean 人声）切参考 wav，写到
    *out_dir*/{speaker}.wav。返回 {speaker_id: path}。无法取到的说话人跳过。"""
    speech_audio_path = Path(speech_audio_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    speakers = sorted({s.get("speaker_id") for s in segments if s.get("speaker_id")})
    refs: dict[str, Path] = {}
    for spk in speakers:
        win = pick_reference_window(segments, spk, min_s=min_s, max_s=max_s)
        if win is None:
            logger.warning("[voiceclone-ref] no usable window for speaker %s", spk)
            continue
        start_ms, end_ms = win
        clip = out_dir / f"{spk}.wav"
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{start_ms / 1000:.3f}",
            "-i", str(speech_audio_path),
            "-t", f"{(end_ms - start_ms) / 1000:.3f}",
            "-ac", "1", "-ar", str(sample_rate),
            str(clip),
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=120)
            if clip.exists() and clip.stat().st_size > 0:
                refs[spk] = clip
        except Exception as exc:
            logger.warning("[voiceclone-ref] ffmpeg failed for %s: %s", spk, exc)
    return refs
```

- [x] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_voiceclone_reference.py -q`
Expected: PASS（2 条）

- [x] **Step 5: Commit**

```bash
git add src/services/tts/voiceclone_reference.py tests/test_voiceclone_reference.py
git commit -m "feat(mimo): per-speaker clean reference extraction from speech_for_asr.wav [free-tier Phase 1]"
```

---

## Task 3：内部 spike 测量 harness

**Files:**
- Create: `scripts/spike/mimo_voiceclone_spike.py`
- Test: `tests/test_mimo_voiceclone_spike.py`

**用途：** 开发者在美国主机手动跑，针对一个真实 job_dir 验证质量 + 量化延迟/失败率/usage。**不进 CI、不自动调用**（付费 API 合规——MiMo 现免费，但仍仅手动触发）。

- [x] **Step 1: 写 smoke 测试（mock provider，不打真实 API）**

```python
# tests/test_mimo_voiceclone_spike.py
import json
from pathlib import Path
import scripts.spike.mimo_voiceclone_spike as spike


def test_run_spike_smoke(tmp_path, monkeypatch):
    # 构造最小 job_dir：translation/segments.json + audio/speech_for_asr.wav(占位)
    (tmp_path / "translation").mkdir()
    (tmp_path / "audio").mkdir()
    (tmp_path / "audio" / "speech_for_asr.wav").write_bytes(b"\x00" * 100)
    segs = {"segments": [
        {"segment_id": 1, "speaker_id": "speaker_a", "start_ms": 0, "end_ms": 6000, "cn_text": "你好测试"},
    ]}
    (tmp_path / "translation" / "segments.json").write_text(json.dumps(segs), encoding="utf-8")
    # mock 参考提取 + voiceclone（不依赖 ffmpeg / 网络）
    monkeypatch.setattr(spike, "extract_speaker_references",
                        lambda *a, **k: {"speaker_a": tmp_path / "ref.wav"})
    (tmp_path / "ref.wav").write_bytes(b"\x00" * 200)
    monkeypatch.setattr(spike, "synthesize_voiceclone", lambda *a, **k: b"\x00" * 5000)
    report = spike.run_spike(str(tmp_path), max_segments=1, out_dir=str(tmp_path / "spike_out"))
    assert report["attempted"] == 1
    assert report["succeeded"] == 1
    assert report["results"][0]["out_bytes"] == 5000
    assert (tmp_path / "spike_out").exists()
```

- [x] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_mimo_voiceclone_spike.py -q`
Expected: FAIL（模块未定义）

- [x] **Step 3: 实现 harness**

```python
# scripts/spike/mimo_voiceclone_spike.py
"""免费版 Phase 1 内部 spike：对真实 job_dir 跑 MiMo voiceclone，记录
延迟/失败/usage/输出。手动运行，需 MIMO_API_KEY。不进 CI、不自动调用。

用法：
  python -m scripts.spike.mimo_voiceclone_spike <job_dir> --max-segments 8
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from services.tts.mimo_tts_provider import synthesize_voiceclone
from services.tts.voiceclone_reference import extract_speaker_references


def _load_segments(job_dir: Path) -> list[dict]:
    raw = json.loads((job_dir / "translation" / "segments.json").read_text(encoding="utf-8"))
    return raw.get("segments", raw if isinstance(raw, list) else [])


def run_spike(job_dir: str, *, max_segments: int = 8, out_dir: str | None = None) -> dict:
    job = Path(job_dir)
    out = Path(out_dir or (job / "reports" / "voiceclone_spike"))
    out.mkdir(parents=True, exist_ok=True)
    segments = _load_segments(job)
    refs = extract_speaker_references(
        segments, job / "audio" / "speech_for_asr.wav", out / "refs"
    )
    results = []
    targets = [s for s in segments if (s.get("cn_text") or "").strip()][:max_segments]
    for s in targets:
        spk = s.get("speaker_id")
        ref = refs.get(spk)
        rec = {"segment_id": s.get("segment_id"), "speaker_id": spk, "ok": False}
        if ref is None:
            rec["error"] = "no_reference"
            results.append(rec)
            continue
        t0 = time.time()
        try:
            audio = synthesize_voiceclone(s["cn_text"].strip(), reference_audio=ref)
            (out / f"seg_{s.get('segment_id')}.wav").write_bytes(audio)
            rec.update(ok=True, out_bytes=len(audio), latency_s=round(time.time() - t0, 2))
        except Exception as exc:  # noqa: BLE001 — spike 容错记录，不中断批次
            rec.update(error=repr(exc)[:200], latency_s=round(time.time() - t0, 2))
        results.append(rec)
    oks = [r for r in results if r["ok"]]
    lat = sorted(r["latency_s"] for r in oks)
    report = {
        "job_dir": str(job),
        "attempted": len(results),
        "succeeded": len(oks),
        "failed": len(results) - len(oks),
        "latency_p50": lat[len(lat) // 2] if lat else None,
        "latency_max": lat[-1] if lat else None,
        "results": results,
    }
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("job_dir")
    ap.add_argument("--max-segments", type=int, default=8)
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args()
    rep = run_spike(args.job_dir, max_segments=args.max_segments, out_dir=args.out_dir)
    print(json.dumps({k: v for k, v in rep.items() if k != "results"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

**导入约定（已核实）：** 仓库 `tests/test_quality_benchmark_tools.py` 已用 `from scripts.benchmark... import ...`，经 PEP 420 命名空间包工作。所以本任务**加 `scripts/spike/__init__.py`（对齐 `scripts/benchmark/__init__.py`），不要加顶层 `scripts/__init__.py`**（会破坏现有命名空间包布局）。测试 `monkeypatch.setattr(spike, "synthesize_voiceclone", ...)` 能生效，因为 harness 在模块顶部 import 了这两个名字。

- [x] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_mimo_voiceclone_spike.py -q`
Expected: PASS

- [x] **Step 5: Commit**

```bash
git add scripts/spike/mimo_voiceclone_spike.py tests/test_mimo_voiceclone_spike.py
git commit -m "feat(mimo): internal voiceclone spike harness (latency/failure/usage) [free-tier Phase 1]"
```

---

## Task 4：部署 + 跑真实批量（手动，美国主机）

> 这不是代码任务，是 Phase 1 的验收动作。**仅开发者手动触发**（付费 API 合规）。

- [x] **Step 1:** 把三个文件 `docker cp` 进 `aivideotrans-app`（src bind-mount）/ 或 `git archive` 打包上传解包；`docker restart aivideotrans-app`（参照本会话 Phase 2/3 部署手法）。
- [x] **Step 2:** 选 2–3 个不同说话人/语言的真实 job_dir，容器内跑：
  `docker exec aivideotrans-app python -m scripts.spike.mimo_voiceclone_spike <job_dir> --max-segments 8`
- [x] **Step 3:** 拉回 `report.json` + 几条输出 wav，人工评：音色一致性、自然度、**latency p50/max、失败率**、是否撞 10MB（参考已封 5s，应不会）。
- [x] **Step 4:** 把结论写回 design 文档 §1.5 Phase 1（替换"首测结果"为"批量结果"），据此决定是否进 Phase 2。

**验收门槛（建议）：** 失败率低、p50 延迟可接受（<15s）、质量 ≥ CosyVoice（首测已初步满足）。达标 → 进 Phase 2 公开版规划（含 6 个落地 gate + consent/法务）。

---

## 明确不做（Phase 1 边界）
- 不加 `service_mode="free"` 到 gateway 白名单 / 前端类型 / 任务列表
- 不动 credits/pricing_runtime、quota、下载 gate、水印
- 不接公开入口、不面向真实用户
- 不碰 Smart / MiniMax / CosyVoice 现有路径
- 不做 voicedesign（另议）
