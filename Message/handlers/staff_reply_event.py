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
from typing import Dict, Optional, List, Any
from utils.logger_loguru import get_logger

logger = get_logger(__name__)


class StaffReplyEventManager:
    """
    客服回复事件管理器

    使用 asyncio.Event 实现事件通知机制，按 from_uid 管理每个买家的等待事件。
    支持同一买家同时有多个等待事件（连续发送多条消息的场景）。
    """

    # 冷却期配置：人工客服回复后，该用户的新消息延长等待时间
    COOLDOWN_SECONDS = 60  # 冷却期60秒
    EXTENDED_WAIT_SECONDS = 30  # 冷却期内延长等待时间

    def __init__(self):
        # from_uid -> [{"event_id": str, "event": asyncio.Event, "timestamp": float, "loop": asyncio.AbstractEventLoop}]
        self._waiting_events: Dict[str, List[Dict[str, Any]]] = {}
        # from_uid -> last_staff_reply_timestamp (记录人工客服最后回复时间)
        self._staff_reply_times: Dict[str, float] = {}
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
            # 记录人工客服回复时间（用于冷却期判断）
            self._staff_reply_times[from_uid] = time.time()
            
            if from_uid not in self._waiting_events:
                logger.debug(f"买家 {from_uid} 没有在等待客服回复")
                return False

            event_list = self._waiting_events[from_uid]
            logger.debug(f"买家 {from_uid} 有 {len(event_list)} 个等待事件")
            
            for event_info in event_list:
                event = event_info.get("event")
                event_id = event_info.get("event_id")
                logger.debug(f"检查事件 {event_id}: event={event}, is_set={event.is_set() if event else None}")

                if event is None:
                    # 事件对象还未创建，说明等待还没开始
                    logger.debug(f"买家 {from_uid} 的事件 {event_id} 对象未创建，跳过通知")
                    continue

                # 设置事件，通知等待者
                event.set()
                notified_count += 1
                logger.debug(f"已通知事件 {event_id}")

            if notified_count > 0:
                logger.info(f"人工客服已回复，通知 {from_uid} 的 {notified_count} 个等待事件")

            return notified_count > 0

    def is_in_cooldown(self, from_uid: str) -> bool:
        """
        检查该用户是否在人工回复冷却期内

        Args:
            from_uid: 买家ID

        Returns:
            bool: True表示在冷却期内（应跳过AI处理），False表示不在冷却期
        """
        with self._lock:
            if from_uid not in self._staff_reply_times:
                return False
            
            last_reply_time = self._staff_reply_times[from_uid]
            elapsed = time.time() - last_reply_time
            
            if elapsed < self.COOLDOWN_SECONDS:
                logger.info(f"买家 {from_uid} 在人工回复冷却期内（剩余 {self.COOLDOWN_SECONDS - elapsed:.1f}秒），跳过AI处理")
                return True
            
            # 冷却期已过，清理记录
            del self._staff_reply_times[from_uid]
            return False

    def get_extended_wait_time(self, from_uid: str) -> float:
        """
        获取该用户的延长等待时间（如果在冷却期内）

        Args:
            from_uid: 买家ID

        Returns:
            float: 延长等待时间（秒），如果不在冷却期内返回0
        """
        with self._lock:
            if from_uid not in self._staff_reply_times:
                return 0.0
            
            last_reply_time = self._staff_reply_times[from_uid]
            elapsed = time.time() - last_reply_time
            
            if elapsed < self.COOLDOWN_SECONDS:
                logger.debug(f"买家 {from_uid} 在冷却期内，延长等待时间至 {self.EXTENDED_WAIT_SECONDS}秒")
                return float(self.EXTENDED_WAIT_SECONDS)
            
            # 冷却期已过，清理记录
            del self._staff_reply_times[from_uid]
            return 0.0

    async def wait_for_staff_reply(self, from_uid: str, event_id: str, timeout: float, auto_cleanup: bool = True) -> bool:
        """
        等待人工客服回复

        Args:
            from_uid: 买家ID
            event_id: 本次等待的事件ID（从start_waiting获取）
            timeout: 超时时间（秒）
            auto_cleanup: 是否自动清理事件（默认True，向后兼容）

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
            # 清理当前事件（可选）
            if auto_cleanup:
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
        expired_count = 0

        with self._lock:
            for from_uid in list(self._waiting_events.keys()):
                events = self._waiting_events[from_uid]
                # 过滤掉过期的事件
                original_count = len(events)
                events[:] = [e for e in events if current_time - e["timestamp"] <= max_age]
                expired_count += original_count - len(events)

                # 如果列表为空，删除整个条目
                if not events:
                    del self._waiting_events[from_uid]

        if expired_count:
            logger.info(f"清理过期等待事件: {expired_count}个")

        return expired_count


# 全局单例
staff_reply_event_manager = StaffReplyEventManager()
