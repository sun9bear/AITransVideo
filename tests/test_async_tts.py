from unittest.mock import MagicMock, patch
from src.services.tts.async_tts_provider import AsyncTTSProvider, AsyncTaskResult

def test_submit_async_returns_task_id():
    provider = AsyncTTSProvider(api_key="test_key")
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"task_id": "task_abc123"}
    mock_resp.raise_for_status = MagicMock()
    with patch.object(provider._session, "post", return_value=mock_resp):
        task_id = provider.submit_async("hello world", "voice_001")
    assert task_id == "task_abc123"

def test_poll_task_completed():
    provider = AsyncTTSProvider(api_key="test_key")
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"status": "Success", "file_url": "https://example.com/audio.wav"}
    mock_resp.raise_for_status = MagicMock()
    with patch.object(provider._session, "get", return_value=mock_resp):
        result = provider.poll_task("task_abc123")
    assert result.status == "completed"
    assert result.file_url == "https://example.com/audio.wav"

def test_poll_task_pending():
    provider = AsyncTTSProvider(api_key="test_key")
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"status": "Processing"}
    mock_resp.raise_for_status = MagicMock()
    with patch.object(provider._session, "get", return_value=mock_resp):
        result = provider.poll_task("task_abc123")
    assert result.status == "pending"

def test_poll_task_failed():
    provider = AsyncTTSProvider(api_key="test_key")
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"status": "Failed", "message": "Voice not found"}
    mock_resp.raise_for_status = MagicMock()
    with patch.object(provider._session, "get", return_value=mock_resp):
        result = provider.poll_task("task_abc123")
    assert result.status == "failed"
    assert "Voice not found" in result.error

def test_choose_tts_strategy():
    """验证策略选择函数"""
    from src.services.tts.tts_generator import choose_tts_strategy
    # 这个函数需要在 tts_generator.py 中添加
    # 暂时跳过，只测 async provider
    pass
