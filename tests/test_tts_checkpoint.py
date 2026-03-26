import os
import time
import tempfile

def test_rate_limiter_enforces_interval():
    from src.services.tts.rate_limiter import RateLimiter
    limiter = RateLimiter(rpm=120)  # 0.5 秒间隔，方便测试
    t1 = time.time()
    limiter.wait()
    limiter.wait()
    limiter.wait()
    t2 = time.time()
    # 3 次调用，间隔至少 1 秒（2 个间隔 × 0.5s）
    assert t2 - t1 >= 0.9

def test_is_valid_output_for_checkpoint():
    from src.utils.atomic_io import is_valid_output
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "segment_001.wav")
        # 不存在
        assert is_valid_output(path) is False
        # 空文件
        with open(path, "wb") as f:
            pass
        assert is_valid_output(path) is False
        # 有内容
        with open(path, "wb") as f:
            f.write(b"audio data")
        assert is_valid_output(path) is True
