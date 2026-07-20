# -*- coding: utf-8 -*-
"""
公共日志模块
提供线程安全的内存日志，所有子线程通过此模块记录关键事件。
"""

import logging
import threading
from datetime import datetime

_LOCK = threading.Lock()
_LOG_LINES: list[str] = []
_MAX_LINES = 2000


def _timestamp() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def log(tag: str, message: str, level: str = "INFO") -> None:
    """
    线程安全地写入内存日志。

    :param tag: 模块标识，如 "ScreenSender"
    :param message: 日志内容
    :param level: INFO / WARN / ERROR / DEBUG
    """
    line = f"[{_timestamp()}] [{level}] [{tag}] {message}"
    with _LOCK:
        _LOG_LINES.append(line)
        if len(_LOG_LINES) > _MAX_LINES:
            _LOG_LINES.pop(0)
    # 同时输出到标准输出（开发调试用）
    print(line)


def get_all_lines() -> list[str]:
    """返回当前全部日志行（副本）。"""
    with _LOCK:
        return list(_LOG_LINES)


def clear() -> None:
    """清空日志缓冲区。"""
    with _LOCK:
        _LOG_LINES.clear()


def error(tag: str, message: str) -> None:
    log(tag, message, "ERROR")


def warn(tag: str, message: str) -> None:
    log(tag, message, "WARN")


def debug(tag: str, message: str) -> None:
    log(tag, message, "DEBUG")
