"""
人工客服消息缓存
按买家UID缓存客服消息，供AI回复时作为上下文注入
"""

import time
from typing import Dict, List, Optional, Tuple
from utils.logger_loguru import get_logger

logger = get_logger(__name__)


class StaffMessageCache:
    """缓存人工客服消息，按买家UID隔离

    当人工客服回复买家后，消息会被缓存。下次同一买家触发AI回复时，
    缓存的客服消息会带时间戳拼接到AI的input中作为上下文。
    """

    def __init__(self, max_messages_per_buyer: int = 20, ttl_seconds: int = 3600):
        """
        Args:
            max_messages_per_buyer: 每个买家最多缓存的消息条数
            ttl_seconds: 缓存过期时间（秒），默认1小时
        """
        self._cache: Dict[str, List[Tuple[float, str]]] = {}  # buyer_uid -> [(timestamp, content)]
        self._max_messages = max_messages_per_buyer
        self._ttl = ttl_seconds

    def add_message(self, buyer_uid: str, content: str) -> None:
        """添加客服消息到缓存

        Args:
            buyer_uid: 买家UID（MALL_CS消息的to_uid）
            content: 客服消息内容
        """
        if not buyer_uid or not content:
            return

        now = time.time()
        if buyer_uid not in self._cache:
            self._cache[buyer_uid] = []

        self._cache[buyer_uid].append((now, content))

        # 超出上限时移除最旧的消息
        if len(self._cache[buyer_uid]) > self._max_messages:
            self._cache[buyer_uid] = self._cache[buyer_uid][-self._max_messages:]

        logger.debug(f"缓存客服消息: buyer_uid={buyer_uid}, 内容={content[:30]}...")

    def get_messages(self, buyer_uid: str) -> List[Tuple[str, str]]:
        """获取买家的客服消息（自动清理过期消息）

        Args:
            buyer_uid: 买家UID

        Returns:
            [(time_str, content)] 时间格式为 HH:MM
        """
        if buyer_uid not in self._cache:
            return []

        now = time.time()
        # 过滤过期消息
        self._cache[buyer_uid] = [
            (ts, content) for ts, content in self._cache[buyer_uid]
            if now - ts < self._ttl
        ]

        if not self._cache[buyer_uid]:
            del self._cache[buyer_uid]
            return []

        # 转换时间戳为 HH:MM 格式
        result = []
        for ts, content in self._cache[buyer_uid]:
            time_str = time.strftime("%H:%M", time.localtime(ts))
            result.append((time_str, content))

        return result

    def clear(self, buyer_uid: Optional[str] = None) -> None:
        """清除缓存

        Args:
            buyer_uid: 指定买家UID则清除该买家的缓存，None则清除全部
        """
        if buyer_uid is None:
            self._cache.clear()
        elif buyer_uid in self._cache:
            del self._cache[buyer_uid]


# 全局单例
staff_message_cache = StaffMessageCache()
