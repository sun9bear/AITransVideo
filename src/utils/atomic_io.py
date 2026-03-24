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
    os.replace(tmp_path, target_path)

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
