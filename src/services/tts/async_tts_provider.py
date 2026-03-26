"""MiniMax T2A Async V2 异步长文本语音合成。"""
import logging
import time
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

@dataclass
class AsyncTaskResult:
    status: str  # "pending", "completed", "failed"
    file_url: str | None = None
    error: str | None = None

class AsyncTTSProvider:
    """MiniMax 异步 TTS 提供者。

    工作流:
    1. submit_async() 提交文本 → 返回 task_id
    2. poll_task() 查询状态 → 返回 AsyncTaskResult
    3. wait_for_completion() 轮询直到完成 → 返回 AsyncTaskResult
    """

    BASE_URL = "https://api.minimaxi.com/v1"

    def __init__(self, api_key: str, model: str = "speech-2.8-turbo"):
        self.api_key = api_key
        self.model = model
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        })

    def submit_async(self, text: str, voice_id: str, **kwargs) -> str:
        """提交异步 TTS 任务，返回 task_id。"""
        body = {
            "model": self.model,
            "text": text,
            "voice_setting": {
                "voice_id": voice_id,
                "speed": kwargs.get("speed", 1.0),
                "vol": kwargs.get("vol", 1.0),
                "pitch": kwargs.get("pitch", 0),
            },
            "audio_setting": {
                "format": kwargs.get("format", "wav"),
                "sample_rate": kwargs.get("sample_rate", 32000),
            },
        }
        resp = self._session.post(
            f"{self.BASE_URL}/t2a_async_v2",
            json=body,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        task_id = data.get("task_id")
        if not task_id:
            raise ValueError(f"异步 TTS 未返回 task_id: {data}")
        logger.info("异步 TTS 任务已提交: %s", task_id)
        return task_id

    def poll_task(self, task_id: str) -> AsyncTaskResult:
        """查询异步任务状态。"""
        resp = self._session.get(
            f"{self.BASE_URL}/query/t2a_async_query_v2",
            params={"task_id": task_id},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        status = data.get("status", "")

        if status == "Success":
            file_url = data.get("file_url") or data.get("file_id")
            return AsyncTaskResult(status="completed", file_url=file_url)
        elif status in ("Failed", "Error"):
            return AsyncTaskResult(
                status="failed",
                error=data.get("message") or data.get("error") or "Unknown error",
            )
        return AsyncTaskResult(status="pending")

    def wait_for_completion(
        self,
        task_id: str,
        interval: int = 10,
        max_wait: int = 3600,
    ) -> AsyncTaskResult:
        """轮询直到任务完成或超时。"""
        start = time.time()
        while time.time() - start < max_wait:
            result = self.poll_task(task_id)
            if result.status != "pending":
                elapsed = int(time.time() - start)
                logger.info("异步 TTS %s 完成，耗时 %ds", task_id, elapsed)
                return result
            time.sleep(interval)
        return AsyncTaskResult(status="failed", error="异步 TTS 轮询超时")

    def download_audio(self, file_url: str) -> bytes:
        """下载完成的音频文件。URL 有效期 9 小时。"""
        resp = self._session.get(file_url, timeout=120)
        resp.raise_for_status()
        return resp.content
