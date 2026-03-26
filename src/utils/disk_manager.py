"""磁盘空间管理。预检 + 中间文件清理。"""
import logging
import os
import shutil

logger = logging.getLogger(__name__)


def check_disk_space(required_gb: float, path: str = "/opt/aivideotrans") -> bool:
    """检查磁盘空间是否充足（需要 1.5 倍余量）。"""
    try:
        usage = shutil.disk_usage(path)
        free_gb = usage.free / (1024 ** 3)
        return free_gb >= required_gb * 1.5
    except OSError:
        return True  # 无法检查时放行


def estimate_required_gb(video_duration_min: float) -> float:
    """估算视频处理所需磁盘空间（GB）。约 35 MB/分钟。"""
    return video_duration_min * 0.035


def cleanup_intermediate(project_dir: str, completed_stage: str) -> int:
    """阶段完成后清理不再需要的中间文件。返回清理的文件数。"""
    removable = {
        "transcription_done": ["audio/original_upload.mp3"],
        "tts_done": [],  # TTS 段文件保留用于对齐
        "output_done": ["audio/speech_for_asr.wav"],
    }
    count = 0
    for relpath in removable.get(completed_stage, []):
        full = os.path.join(project_dir, relpath)
        if os.path.isfile(full):
            try:
                os.remove(full)
                count += 1
                logger.info("清理中间文件: %s", relpath)
            except OSError:
                logger.warning("清理失败: %s", relpath)
    return count


def get_project_size_mb(project_dir: str) -> float:
    """计算项目目录总大小（MB）。"""
    total = 0
    for root, _, files in os.walk(project_dir):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total / (1024 * 1024)
