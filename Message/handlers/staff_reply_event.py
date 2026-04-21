"""
客服回复事件管理器

用于协调人工客服优先回复功能：
- 收到客户消息后，等待人工客服回复
- 如果人工客服在指定时间内回复，取消AI处理
- 如果超时，继续AI处理流程

基于上游新架构重实现
"""

import asyncio
import time
import threading
import uuid
from typing import Dict, Optional, List
from utils.logger_loguru import get_logger

logger = get_logger(__name__)


class StaffReplyEventManager:
    """
    客服回复事件管理器

    使用 asyncio.Event 实现事件通知机制，按 from_uid 管理每个买家的等待事件。
    支持同一买家同时有多个等待事件（连续发送多条消息的场景）。
    """

    def __init__(self):
        # from_uid -> [{"event_id": str, "event": asyncio.Event, "timestamp": float, "loop": asyncio.AbstractEventLoop}]
        self._waiting_events: Dict[str, List[dict]] = {}
        self._lock = threading.Lock()

    def start_waiting(self, from_uid: str) -> str:
        """
        开始等待客服回复

        Args:
            from_uid: 买家ID

        Returns:
            str: 本次等待的唯一事件ID，用于后续停止等待
        """
        event_id = str(uuid.uuid4())
        with self._lock:
            if from_uid not in self._waiting_events:
                self._waiting_events[from_uid] = []

            # 添加新的事件记录（延迟到实际使用时绑定到当前 event loop）
            self._waiting_events[from_uid].append({
                "event_id": event_id,
                "event": None,  # 惰性初始化
                "timestamp": time.time(),
                "loop": None  # 记录创建时的 event loop
            })
            logger.debug(f"开始等待客服回复: {from_uid}, 事件ID: {event_id}, 当前等待数: {len(self._waiting_events[from_uid])}")
            return event_id

    def stop_waiting(self, from_uid: str, event_id: str) -> None:
        """
        停止指定的等待客服回复事件（清理资源）

        Args:
            from_uid: 买家ID
            event_id: 要停止的事件ID
        """
        with self._lock:
            if from_uid not in self._waiting_events:
                return

            # 查找并删除指定的事件
            event_list = self._waiting_events[from_uid]
            for i, event_info in enumerate(event_list):
                if event_info["event_id"] == event_id:
                    del event_list[i]
                    logger.debug(f"停止等待客服回复: {from_uid}, 事件ID: {event_id}, 剩余等待数: {len(event_list)}")
                    break

            # 如果该用户没有等待事件了，删除整个key
            if not event_list:
                del self._waiting_events[from_uid]

    def notify_staff_reply(self, from_uid: str) -> bool:
        """
        通知人工客服已回复，会通知该买家的所有等待事件

        Args:
            from_uid: 买家ID

        Returns:
            bool: 是否成功通知（如果没有人在等待，返回False）
        """
        notified_count = 0
        with self._lock:
            if from_uid not in self._waiting_events:
                logger.debug(f"买家 {from_uid} 没有在等待客服回复")
                return False

            event_list = self._waiting_events[from_uid]
            for event_info in event_list:
                event = event_info.get("event")

                if event is None:
                    # 事件对象还未创建，说明等待还没开始
                    logger.debug(f"买家 {from_uid} 的事件 {event_info['event_id']} 对象未创建，跳过通知")
                    continue

                # 设置事件，通知等待者
                event.set()
                notified_count += 1

            if notified_count > 0:
                logger.info(f"人工客服已回复，通知 {from_uid} 的 {notified_count} 个等待事件")

            return notified_count > 0

    async def wait_for_staff_reply(self, from_uid: str, event_id: str, timeout: float) -> bool:
        """
        等待人工客服回复

        Args:
            from_uid: 买家ID
            event_id: 本次等待的事件ID（从start_waiting获取）
            timeout: 超时时间（秒）

        Returns:
            bool: True表示人工客服已回复，False表示超时
        """
        # 获取或创建事件对象（确保绑定到当前 event loop）
        event = None
        with self._lock:
            if from_uid not in self._waiting_events:
                logger.warning(f"买家 {from_uid} 未调用 start_waiting")
                return False

            # 查找对应的事件
            event_info = None
            for info in self._waiting_events[from_uid]:
                if info["event_id"] == event_id:
                    event_info = info
                    break

            if not event_info:
                logger.warning(f"事件 {event_id} 不存在，可能已经被清理")
                return False

            # 惰性初始化事件对象（确保绑定到当前 event loop）
            if event_info["event"] is None:
                event_info["event"] = asyncio.Event()
                event_info["loop"] = asyncio.get_event_loop()

            event = event_info["event"]

        logger.debug(f"开始等待 {timeout}秒: {from_uid}, 事件ID: {event_id}")

        try:
            # 等待事件或超时
            await asyncio.wait_for(event.wait(), timeout=timeout)
            logger.info(f"人工客服已回复: {from_uid}, 事件ID: {event_id}")
            return True
        except asyncio.TimeoutError:
            logger.debug(f"等待超时 {timeout}秒: {from_uid}, 事件ID: {event_id}")
            return False
        finally:
            # 清理当前事件
            self.stop_waiting(from_uid, event_id)

    def is_waiting(self, from_uid: str) -> bool:
        """
        检查是否正在等待客服回复

        Args:
            from_uid: 买家ID

        Returns:
            bool: 是否正在等待
        """
        with self._lock:
            return from_uid in self._waiting_events

    def cleanup_expired(self, max_age: float = 300.0) -> int:
        """
        清理过期的等待事件

        Args:
            max_age: 最大存活时间（秒），默认5分钟

        Returns:
            int: 清理的数量
        """
        current_time = time.time()
        expired_uids = []

        with self._lock:
            for from_uid, event_info in list(self._waiting_events.items()):
                if current_time - event_info["timestamp"] > max_age:
                    expired_uids.append(from_uid)

            for uid in expired_uids:
                del self._waiting_events[uid]

        if expired_uids:
            logger.info(f"清理过期等待事件: {len(expired_uids)}个")

        return len(expired_uids)


# 全局单例
staff_reply_event_manager = StaffReplyEventManager()
