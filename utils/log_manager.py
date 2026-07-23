#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
日志文件管理工具

设计原则：
- 不轮转，只保留当前日志文件
- 启动时清理旧的 zip 备份和过期日志
- 当前日志超过阈值时直接清空（不保留备份）
"""

import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Union


# 常见时间戳模式：
# "=== App started at 2026-07-10 12:30:48 ==="
# "--- Timestamp: 2026-07-10 12:30:48 ---"
_TIMESTAMP_PATTERN = re.compile(
    rb"(?:=== App started at|Timestamp:)\s+(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})"
)


def _parse_size(size_str: str) -> int:
    """解析带单位的大小字符串，返回字节数"""
    size_str = size_str.strip().upper()
    multipliers = {
        "KB": 1024,
        "MB": 1024 * 1024,
        "GB": 1024 * 1024 * 1024,
    }
    for suffix, multiplier in multipliers.items():
        if size_str.endswith(suffix):
            try:
                return int(float(size_str[: -len(suffix)].strip()) * multiplier)
            except ValueError:
                break
    # 没有单位或解析失败，按原始字节数处理
    try:
        return int(size_str)
    except ValueError:
        return 0


def _parse_days(days_str: str) -> int:
    """解析天数"""
    try:
        days = int(days_str)
        return max(days, 0)
    except ValueError:
        return 7


def clean_log_file(path: Union[str, Path], max_size: Union[str, int]) -> bool:
    """
    当日志文件超过指定大小时直接清空

    Args:
        path: 日志文件路径
        max_size: 最大大小，可以是 "50 MB" 或字节数

    Returns:
        是否执行了清理
    """
    path = Path(path)
    if not path.exists():
        return False

    max_size_bytes = max_size if isinstance(max_size, int) else _parse_size(max_size)
    if max_size_bytes <= 0:
        return False

    try:
        if path.stat().st_size > max_size_bytes:
            path.write_text("", encoding="utf-8")
            return True
    except Exception:
        pass
    return False


def delete_old_files(
    directory: Union[str, Path],
    retention_days: Union[str, int] = 7,
    delete_zip: bool = True,
) -> int:
    """
    删除目录下过期的日志文件和 zip 备份

    Args:
        directory: 要清理的目录
        retention_days: 保留天数
        delete_zip: 是否删除所有 zip 备份

    Returns:
        删除的文件数量
    """
    directory = Path(directory)
    if not directory.exists() or not directory.is_dir():
        return 0

    retention_days = _parse_days(str(retention_days))
    cutoff = datetime.now() - timedelta(days=retention_days)
    removed = 0

    for entry in directory.iterdir():
        if not entry.is_file():
            continue

        try:
            # 直接删除所有 zip 备份（轮转产物）
            if delete_zip and entry.suffix.lower() == ".zip":
                entry.unlink()
                removed += 1
                continue

            # 删除超过保留期的文件
            mtime = datetime.fromtimestamp(entry.stat().st_mtime)
            if mtime < cutoff:
                entry.unlink()
                removed += 1
        except Exception:
            continue

    return removed


def truncate_to_recent_hours(
    path: Union[str, Path],
    hours: Union[str, int] = 1,
    max_size: Union[str, int, None] = None,
) -> bool:
    """
    当日志文件过大时，只保留最近 N 小时的内容

    通过扫描文件中的时间戳（`App started at` 或 `Timestamp:`）
    找到 N 小时前的时间边界，截断保留之后的内容。
    如果没有找到时间戳，且文件超过 max_size，则直接清空。

    Args:
        path: 日志文件路径
        hours: 保留最近多少小时
        max_size: 可选，超过该大小才执行截断；为 None 时只要文件存在就处理

    Returns:
        是否执行了清理
    """
    path = Path(path)
    if not path.exists():
        return False

    hours = _parse_days(str(hours))  # 借用正整数解析
    cutoff = datetime.now() - timedelta(hours=hours)

    # 检查大小条件
    if max_size is not None:
        max_size_bytes = max_size if isinstance(max_size, int) else _parse_size(max_size)
        if max_size_bytes > 0 and path.stat().st_size <= max_size_bytes:
            return False

    data = path.read_bytes()
    positions = [
        (datetime.strptime(m.group(1).decode("utf-8"), "%Y-%m-%d %H:%M:%S"), m.start())
        for m in _TIMESTAMP_PATTERN.finditer(data)
    ]

    if not positions:
        # 没有时间戳，无法定位最近 1 小时，直接清空
        path.write_text("", encoding="utf-8")
        return True

    # 找到第一个 >= cutoff 的时间戳位置
    first_valid_pos: Union[int, None] = None
    for ts, pos in positions:
        if ts >= cutoff:
            first_valid_pos = pos
            break

    if first_valid_pos is None:
        # 所有记录都早于 cutoff，清空
        path.write_text("", encoding="utf-8")
        return True

    if first_valid_pos == 0:
        return False  # 无需截断

    new_data = data[first_valid_pos:]
    path.write_bytes(new_data)
    return True


def clean_directory_logs(
    directory: Union[str, Path],
    retention_days: Union[str, int] = 7,
    max_size: Union[str, int] = "50 MB",
    current_log_name: str = "app.log",
) -> dict:
    """
    清理日志目录：删除旧备份 + 清空过大的当前日志

    Args:
        directory: 日志目录
        retention_days: 保留天数
        max_size: 当前日志最大大小
        current_log_name: 当前日志文件名

    Returns:
        {"removed": 删除文件数, "truncated": 是否截断当前日志}
    """
    directory = Path(directory)
    removed = delete_old_files(directory, retention_days, delete_zip=True)
    truncated = clean_log_file(directory / current_log_name, max_size)
    return {"removed": removed, "truncated": truncated}
