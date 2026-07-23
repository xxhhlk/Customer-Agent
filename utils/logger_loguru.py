#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
基于loguru的日志模块 - 提供全局日志功能，支持结构化日志和异步处理
"""

import os
import sys
import json
import uuid
from datetime import datetime
from typing import Any, Dict, Optional, Union
from pathlib import Path

from loguru import logger

# 导入日志管理工具
from utils.log_manager import delete_old_files

# 可选的PyQt6依赖
try:
    from PyQt6.QtCore import QObject, QTimer, pyqtSignal  # pyright: ignore
    PYQT6_AVAILABLE = True
except ImportError:
    PYQT6_AVAILABLE = False
    # 创建占位符类
    class QObject:  # type: ignore[misc,no-redef]
        """占位符类，当 PyQt6 不可用时使用"""
        def __init__(self, *args, **kwargs):
            pass
    class QTimer:  # type: ignore[misc,no-redef]
        """占位符类，当 PyQt6 不可用时使用"""
        def __init__(self, *args, **kwargs):
            pass
        def setInterval(self, ms: int): pass  # type: ignore[empty-body]
        def start(self): pass  # type: ignore[empty-body]
        def stop(self): pass  # type: ignore[empty-body]
        @property
        def timeout(self):
            class DummyTimeout:
                def connect(self, *args, **kwargs): pass
            return DummyTimeout()
    def pyqtSignal(*args):  # type: ignore[misc,no-redef]
        """占位符信号，当 PyQt6 不可用时使用"""
        class DummySignal:
            def emit(self, *args, **kwargs):
                pass
            def connect(self, *args, **kwargs):
                pass
            def disconnect(self, *args, **kwargs):
                pass
        return DummySignal()

# 默认配置
DEFAULT_LOG_LEVEL = "info"
DEFAULT_LOG_FILE = "logs/app.log"
MAX_LOG_SIZE = os.environ.get("LOG_MAX_SIZE", "50 MB")
LOG_RETENTION_DAYS = int(os.environ.get("LOG_RETENTION_DAYS", "7"))

# 启动时清理过期日志（loguru 会负责轮转保留，这里只删除超过保留期的旧文件/zip）
log_dir = Path(DEFAULT_LOG_FILE).parent
delete_old_files(log_dir, retention_days=LOG_RETENTION_DAYS, delete_zip=True)

# 确保日志目录存在
os.makedirs(os.path.dirname(DEFAULT_LOG_FILE), exist_ok=True)

# 配置loguru
log_level = os.environ.get("LOG_LEVEL", DEFAULT_LOG_LEVEL).lower()

# 移除默认处理器
logger.remove()

# 检查是否在打包环境中
import sys
is_frozen = getattr(sys, 'frozen', False)

# 添加控制台处理器（仅在开发环境）
if not is_frozen:
    logger.add(
        sys.stdout,
        format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        level=log_level.upper(),
        colorize=True,
        backtrace=True,
        diagnose=not is_frozen
    )

# 添加文件处理器（按大小轮转，保留 7 天）
logger.add(
    DEFAULT_LOG_FILE,
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}",
    level=log_level.upper(),
    rotation=MAX_LOG_SIZE,
    retention=f"{LOG_RETENTION_DAYS} days",
    compression="zip",
    encoding="utf-8",
    backtrace=True,
    diagnose=not is_frozen
)

# 全局logger对象（保持向后兼容）
app_logger = logger

def get_logger(name=None):
    """
    获取logger实例

    Args:
        name: logger名称，如果为None则使用调用模块的名称

    Returns:
        loguru logger实例
    """
    if name is None:
        # 获取调用者的模块名
        import inspect
        frame = inspect.currentframe()
        if frame is not None and frame.f_back is not None:
            name = frame.f_back.f_globals.get('__name__', 'unknown')

            # 如果是__main__, 使用文件名
            if name == '__main__':
                filename = frame.f_back.f_globals.get('__file__', 'main')
                name = os.path.splitext(os.path.basename(filename))[0]
        else:
            name = 'unknown'

    # 绑定模块名称到logger
    return logger.bind(module=name)

# 导出全局日志对象和获取logger的函数
__all__ = ["logger", "app_logger", "get_logger", "BusinessLogger", "get_business_logger", "log_with_ctx"]

class BusinessLogger:
    """业务日志记录器，基于loguru实现"""

    def __init__(self, module_name: str):
        self.module_name = module_name
        self.logger = logger.bind(business=True, module=module_name)

    def log_message_process(self, user_id: str, message_type: str, processing_time: float, **kwargs) -> None:
        """记录消息处理事件"""
        self.logger.info(
            "消息处理完成",
            extra={
                "event_type": "message_processed",
                "user_id": user_id,
                "message_type": message_type,
                "processing_time_ms": round(processing_time * 1000, 2),
                **kwargs
            }
        )

    def log_agent_response(self, user_id: str, query_length: int, response_length: int, response_time: float, **kwargs) -> None:
        """记录Agent响应事件"""
        self.logger.info(
            "Agent响应生成",
            extra={
                "event_type": "agent_response",
                "user_id": user_id,
                "query_length": query_length,
                "response_length": response_length,
                "response_time_ms": round(response_time * 1000, 2),
                **kwargs
            }
        )

    def log_error(self, error_type: str, error_message: str, user_id: Optional[str] = None, **kwargs) -> None:
        """记录业务错误"""
        self.logger.error(
            "业务错误",
            extra={
                "event_type": "business_error",
                "error_type": error_type,
                "error_message": error_message,
                "user_id": user_id,
                **kwargs
            }
        )

    def log_performance(self, operation: str, duration: float, **kwargs) -> None:
        """记录性能指标"""
        self.logger.info(
            "性能指标",
            extra={
                "event_type": "performance_metric",
                "operation": operation,
                "duration_ms": round(duration * 1000, 2),
                **kwargs
            }
        )

def get_business_logger(module_name: str) -> BusinessLogger:
    """获取业务日志记录器实例"""
    return BusinessLogger(module_name)

# UI集成部分
class UILogHandler(QObject):  # type: ignore[misc]
    """UI日志处理器，兼容现有LogHandler接口

    **线程安全设计**：
    loguru 的 sink 会被任意线程调用（包括 AutoReplyThread 的 asyncio 事件循环），
    直接从非主线程 emit pyqtSignal 在高频场景下会导致 C 层 access violation。

    解决方案：sink 只将日志写入线程安全的 deque 缓冲区，
    由主线程的 QTimer 定期轮询并批量 emit 信号。
    """

    log_received = pyqtSignal(str, str, object)  # level, message, record

    # 缓冲区最大长度，超出后丢弃最旧的日志
    _MAX_BUFFER = 500
    # 轮询间隔（毫秒）
    _POLL_INTERVAL_MS = 50
    # 每次轮询最多处理的日志条数，避免主线程卡顿
    _BATCH_SIZE = 30

    def __init__(self):
        super().__init__()
        self.handler_id = None
        import threading
        from collections import deque
        self._buffer: deque = deque(maxlen=self._MAX_BUFFER)
        self._buffer_lock = threading.Lock()
        self._timer: Optional["QTimer"] = None
        self._install_loguru_patch()
        self._install_timer()

    def _install_loguru_patch(self):
        """安装loguru拦截器"""
        def ui_sink(message):
            # 解析loguru消息以提取信息
            record = message.record
            level = record["level"].name
            msg = record["message"]
            # 写入线程安全缓冲区，不直接 emit
            try:
                with self._buffer_lock:
                    self._buffer.append((level, msg, record))
            except Exception:
                pass  # 缓冲区操作失败时静默丢弃

        # 安装UI处理器
        self.handler_id = logger.add(ui_sink, level="DEBUG", catch=True)

    def _install_timer(self):
        """安装主线程定时器，定期从缓冲区取日志并 emit"""
        try:
            self._timer = QTimer(self)  # type: ignore[arg-type]
            self._timer.setInterval(self._POLL_INTERVAL_MS)
            self._timer.timeout.connect(self._flush_buffer)
            self._timer.start()
        except Exception:
            # QTimer 初始化失败（如非 GUI 线程构造），退化为直接 emit
            self._timer = None

    def _flush_buffer(self):
        """从缓冲区批量取日志并在主线程 emit"""
        if not self._buffer:
            return
        batch = []
        with self._buffer_lock:
            count = 0
            while self._buffer and count < self._BATCH_SIZE:
                batch.append(self._buffer.popleft())
                count += 1
        for level, msg, record in batch:
            try:
                self.log_received.emit(level, msg, record)
            except Exception:
                pass  # emit 失败时静默丢弃，防止级联崩溃

    def emit(self, record):
        """为了兼容性保留"""
        pass

    def install(self):
        """安装处理器 - 已经在__init__中完成"""
        pass

    def uninstall(self):
        """卸载处理器"""
        if self._timer:
            self._timer.stop()
            self._timer = None
        if self.handler_id:
            logger.remove(self.handler_id)
            self.handler_id = None

# 上下文日志功能
def format_conn_key(shop_id: Optional[str], user_id: Optional[str]) -> str:
    """格式化连接键"""
    if not shop_id or not user_id:
        return "unknown_unknown"
    return f"{shop_id}_{user_id}"

def log_with_ctx(logger_name: str, msg: str, shop_id: Optional[str] = None,
                 user_id: Optional[str] = None, username: Optional[str] = None,
                 from_uid: Optional[str] = None):
    """带上下文的日志记录"""
    context_parts = []
    if shop_id or user_id:
        context_parts.append(f"key={format_conn_key(shop_id, user_id)}")
    if username:
        context_parts.append(f"user={username}")
    if from_uid:
        context_parts.append(f"from_uid={from_uid}")

    context = f"{' '.join(context_parts)} | " if context_parts else ""
    logger.bind(context=context, logger_name=logger_name).info(f"{context}{msg}")