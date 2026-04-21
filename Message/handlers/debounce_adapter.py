"""
防抖处理器适配器
适配 UserSequentialProcessor 到 MessageConsumer
"""

import asyncio
from typing import Optional, Dict, Any
from bridge.context import Context
from utils.logger_loguru import get_logger
from ..models.queue_models import MessageWrapper


logger = get_logger(__name__)


class DebounceProcessorAdapter:
    """防抖处理器适配器 - 简化版"""

    # 防抖配置
    DEBOUNCE_SECONDS = 8  # 白天防抖时间
    NIGHT_DEBOUNCE_SECONDS = 300  # 夜间防抖时间（5分钟）
    NIGHT_START = 23  # 夜间开始时间（23:01）
    NIGHT_END = 7   # 夜间结束时间（07:55）

    def __init__(self):
        self._last_message_time: Dict[str, float] = {}
        self._pending_messages: Dict[str, list] = {}
        self.logger = get_logger("DebounceProcessor")

    async def process_with_debounce(
        self, 
        wrapper: MessageWrapper, 
        user_queue: Optional[asyncio.Queue] = None
    ) -> Optional[MessageWrapper]:
        """
        带防抖的消息处理

        Args:
            wrapper: 消息封装
            user_queue: 用户队列（用于收集后续消息）

        Returns:
            合并后的消息封装，如果被合并则返回 None
        """
        try:
            user_key = self._extract_user_id(wrapper.context)
            current_time = asyncio.get_event_loop().time()

            # 检查是否在防抖窗口内
            last_time = self._last_message_time.get(user_key, 0)
            debounce_seconds = self._get_debounce_seconds()

            if current_time - last_time < debounce_seconds:
                # 在防抖窗口内，合并消息
                self.logger.debug(f"User {user_key} message merged (debounce)")
                self._last_message_time[user_key] = current_time
                return None

            # 更新最后消息时间
            self._last_message_time[user_key] = current_time

            # 等待防抖窗口，收集后续消息
            merged_wrapper = await self._wait_and_merge(wrapper, user_queue, debounce_seconds)

            return merged_wrapper

        except Exception as e:
            self.logger.error(f"Debounce processing error: {e}")
            return wrapper

    async def _wait_and_merge(
        self, 
        wrapper: MessageWrapper, 
        user_queue: Optional[asyncio.Queue],
        debounce_seconds: float
    ) -> MessageWrapper:
        """等待防抖窗口并合并消息"""
        try:
            # 等待防抖时间
            await asyncio.sleep(debounce_seconds)

            # 如果没有队列，直接返回原消息
            if not user_queue:
                return wrapper

            # 尝试收集队列中的后续消息
            messages_to_merge = [wrapper]
            
            while True:
                try:
                    next_wrapper = user_queue.get_nowait()
                    messages_to_merge.append(next_wrapper)
                except asyncio.QueueEmpty:
                    break

            # 如果只有一条消息，直接返回
            if len(messages_to_merge) == 1:
                return wrapper

            # 合并多条消息
            self.logger.info(f"Merged {len(messages_to_merge)} messages")
            return self._merge_messages(messages_to_merge)

        except Exception as e:
            self.logger.error(f"Wait and merge error: {e}")
            return wrapper

    def _merge_messages(self, wrappers: list) -> MessageWrapper:
        """合并多条消息"""
        # 使用最后一条消息的上下文
        last_wrapper = wrappers[-1]
        merged_context = last_wrapper.context

        # 合并消息内容
        if merged_context.type.name == "TEXT":
            texts = []
            for w in wrappers:
                if w.context.type.name == "TEXT" and w.context.content:
                    texts.append(w.context.content)
            
            if texts:
                merged_context.content = "\n".join(texts)

        return last_wrapper

    def _get_debounce_seconds(self) -> float:
        """获取当前应该使用的防抖等待时间"""
        import time
        current_hour = time.localtime().tm_hour

        # 检查是否在夜间时段
        if current_hour >= self.NIGHT_START or current_hour <= self.NIGHT_END:
            return self.NIGHT_DEBOUNCE_SECONDS
        else:
            return self.DEBOUNCE_SECONDS

    def _extract_user_id(self, context: Context) -> str:
        """提取用户ID"""
        try:
            from_uid = context.kwargs.from_uid if hasattr(context, 'kwargs') else None
            channel = context.channel_type

            if from_uid is None:
                from_uid = "unknown"
            if channel is None:
                channel = "unknown"

            if hasattr(channel, 'value'):
                channel_str = str(channel.value)
            else:
                channel_str = str(channel)

            return f"{channel_str}_{from_uid}"
        except Exception as e:
            self.logger.error(f"Failed to extract user ID: {e}")
            return "unknown_unknown"


# 全局实例
debounce_processor = DebounceProcessorAdapter()
