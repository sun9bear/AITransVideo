"""TTS API 限速器。确保请求频率不超过 RPM 限制。"""
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
