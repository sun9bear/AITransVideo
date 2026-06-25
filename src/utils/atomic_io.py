"""原子写入工具。写入 .tmp 文件后原子重命名，防止半写入。"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_bytes(target_path: str | Path, data: bytes) -> None:
    """将字节数据原子写入目标文件（tempfile + os.replace）。"""
    target = Path(target_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_str = tempfile.mkstemp(
        prefix=target.name + ".",
        suffix=".tmp",
        dir=str(target.parent),
    )
    tmp_path = Path(tmp_str)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target)
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def atomic_write_json(
    target_path: str | Path,
    data: Any,
    *,
    fsync: bool = True,
    sort_keys: bool = True,
    trailing_newline: bool = False,
) -> None:
    """将 JSON 可序列化对象原子写入目标文件。

    参数
    ----
    target_path : str | Path
        目标文件路径（父目录不存在时自动创建）。
    data : Any
        可 json.dumps 的对象（dict / list / 其他 JSON 类型）。
    fsync : bool
        True（默认）：rename 前先 fsync，保证内容落盘——适用于业务状态文件
        （segments.json / voice_map.json / manifest 等）。
        False：跳过 fsync，仅保证 rename 原子性——适用于 JobStore group-commit
        快速路径（见 store.py _write_json_atomic 注释）。
    sort_keys : bool
        True（默认）：键排序，利于 diff / 调试。
        editing_segments / editing_voice_map 迁移时必须传 False，保持原有字节顺序。
    trailing_newline : bool
        False（默认）。editing_voice_map / review_actions 原实现末尾有 \\n，
        迁移时按调用点需要传 True（内容等价）。
    """
    target = Path(target_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=sort_keys)
    if trailing_newline:
        serialized += "\n"
    encoded = serialized.encode("utf-8")
    fd, tmp_str = tempfile.mkstemp(
        prefix=target.name + ".",
        suffix=".tmp",
        dir=str(target.parent),
    )
    tmp_path = Path(tmp_str)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(encoded)
            f.flush()
            if fsync:
                os.fsync(f.fileno())
        os.replace(tmp_path, target)
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def is_valid_output(path: str | Path) -> bool:
    """检查文件是否存在且非空（用于 checkpoint 判断）"""
    p = str(path)
    return os.path.isfile(p) and os.path.getsize(p) > 0


def cleanup_tmp_files(directory: str | Path) -> int:
    """清理目录下所有 .tmp 文件，返回清理数量"""
    count = 0
    for root, _, files in os.walk(str(directory)):
        for f in files:
            if f.endswith(".tmp"):
                os.remove(os.path.join(root, f))
                count += 1
    return count
