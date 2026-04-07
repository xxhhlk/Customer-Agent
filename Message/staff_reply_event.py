"""
客服回复事件管理器

用于协调人工客服优先回复功能：
- 收到客户消息后，等待人工客服回复
- 如果人工客服在指定时间内回复，取消AI处理
- 如果超时，继续AI处理流程
"""

import asyncio
import time
import threading
from typing import Dict, Optional
from utils.logger import logger


class StaffReplyEventManager:
    """
    客服回复事件管理器
    
    使用 asyncio.Event 实现事件通知机制，按 from_uid 管理每个买家的等待事件。
    """
    
    def __init__(self):
        # from_uid -> {"event": asyncio.Event, "timestamp": float}
        self._waiting_events: Dict[str, dict] = {}
        self._lock = threading.Lock()
    
    def start_waiting(self, from_uid: str) -> None:
        """
        开始等待客服回复
        
        Args:
            from_uid: 买家ID
        """
        with self._lock:
            if from_uid in self._waiting_events:
                logger.warning(f"[StaffReplyEvent] 买家 {from_uid} 已在等待中，重置事件")
            
            # 创建新的事件对象（延迟到实际使用时绑定到当前 event loop）
            self._waiting_events[from_uid] = {
                "event": None,  # 惰性初始化
                "timestamp": time.time(),
                "loop": None  # 记录创建时的 event loop
            }
            logger.debug(f"[StaffReplyEvent] 开始等待客服回复: {from_uid}")
    
    def stop_waiting(self, from_uid: str) -> None:
        """
        停止等待客服回复（清理资源）
        
        Args:
            from_uid: 买家ID
        """
        with self._lock:
            if from_uid in self._waiting_events:
                del self._waiting_events[from_uid]
                logger.debug(f"[StaffReplyEvent] 停止等待客服回复: {from_uid}")
    
    def notify_staff_reply(self, from_uid: str) -> bool:
        """
        通知人工客服已回复
        
        Args:
            from_uid: 买家ID
            
        Returns:
            bool: 是否成功通知（如果没有人在等待，返回False）
        """
        with self._lock:
            if from_uid not in self._waiting_events:
                logger.debug(f"[StaffReplyEvent] 买家 {from_uid} 没有在等待客服回复")
                return False
            
            event_info = self._waiting_events[from_uid]
            event = event_info.get("event")
            
            if event is None:
                # 事件对象还未创建，说明等待还没开始
                logger.debug(f"[StaffReplyEvent] 买家 {from_uid} 的事件对象未创建")
                return False
            
            # 设置事件，通知等待者
            event.set()
            logger.info(f"[StaffReplyEvent] 人工客服已回复，通知等待者: {from_uid}")
            return True
    
    async def wait_for_staff_reply(self, from_uid: str, timeout: float) -> bool:
        """
        等待人工客服回复
        
        Args:
            from_uid: 买家ID
            timeout: 超时时间（秒）
            
        Returns:
            bool: True表示人工客服已回复，False表示超时
        """
        # 获取或创建事件对象（确保绑定到当前 event loop）
        event = None
        with self._lock:
            if from_uid not in self._waiting_events:
                logger.warning(f"[StaffReplyEvent] 买家 {from_uid} 未调用 start_waiting")
                return False
            
            event_info = self._waiting_events[from_uid]
            
            # 惰性初始化事件对象（确保绑定到当前 event loop）
            if event_info["event"] is None:
                event_info["event"] = asyncio.Event()
                event_info["loop"] = asyncio.get_event_loop()
            
            event = event_info["event"]
        
        logger.debug(f"[StaffReplyEvent] 开始等待 {timeout}秒: {from_uid}")
        
        try:
            # 等待事件或超时
            await asyncio.wait_for(event.wait(), timeout=timeout)
            logger.info(f"[StaffReplyEvent] 人工客服已回复: {from_uid}")
            return True
        except asyncio.TimeoutError:
            logger.debug(f"[StaffReplyEvent] 等待超时 {timeout}秒: {from_uid}")
            return False
        finally:
            # 清理事件
            self.stop_waiting(from_uid)
    
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
            logger.info(f"[StaffReplyEvent] 清理过期等待事件: {len(expired_uids)}个")
        
        return len(expired_uids)


# 全局单例
staff_reply_event_manager = StaffReplyEventManager()
