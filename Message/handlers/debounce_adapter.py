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
        user_queue: Optional[asyncio.Queue] = None,
        staff_reply_manager=None,
        from_uid=None,
        event_id=None
    ) -> Optional[MessageWrapper]:
        """
        带防抖的消息处理

        Args:
            wrapper: 消息封装
            user_queue: 用户队列（用于收集后续消息）
            staff_reply_manager: 人工回复事件管理器
            from_uid: 用户ID
            event_id: 事件ID

        Returns:
            合并后的消息封装，如果被合并则返回 None，如果人工回复则返回 None
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

            # 等待防抖窗口，收集后续消息，同时监听人工回复
            merged_wrapper = await self._wait_and_merge(
                wrapper, 
                user_queue, 
                debounce_seconds,
                staff_reply_manager,
                from_uid,
                event_id
            )

            return merged_wrapper

        except Exception as e:
            self.logger.error(f"Debounce processing error: {e}")
            return wrapper

    async def _wait_and_merge(
        self, 
        wrapper: MessageWrapper, 
        user_queue: Optional[asyncio.Queue],
        debounce_seconds: float,
        staff_reply_manager=None,
        from_uid=None,
        event_id=None
    ) -> Optional[MessageWrapper]:
        """等待防抖窗口并合并消息，同时监听人工回复事件"""
        try:
            # 如果提供了人工回复管理器和事件ID，则同时监听人工回复
            if staff_reply_manager and from_uid and event_id:
                # 创建防抖超时任务
                debounce_task = asyncio.create_task(asyncio.sleep(debounce_seconds))
                
                # 创建人工回复监听任务
                staff_reply_task = asyncio.create_task(
                    staff_reply_manager.wait_for_staff_reply(from_uid, event_id, timeout=debounce_seconds)
                )
                
                # 等待任一任务完成
                done, pending = await asyncio.wait(
                    {debounce_task, staff_reply_task},
                    return_when=asyncio.FIRST_COMPLETED
                )
                
                # 取消未完成的任务
                for task in pending:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                
                # 检查是哪个任务完成了
                if staff_reply_task in done:
                    # 人工回复了，检查结果
                    try:
                        staff_replied = staff_reply_task.result()
                        if staff_replied:
                            self.logger.info(f"人工客服在防抖期间回复了 {from_uid}，取消自动回复")
                            # 清理等待事件
                            staff_reply_manager.stop_waiting(from_uid, event_id)
                            return None  # 返回None表示取消自动回复
                    except Exception as e:
                        self.logger.error(f"检查人工回复结果时出错: {e}")
                
                # 如果是防抖超时完成，继续下面的正常流程
            else:
                # 没有提供人工回复管理器，使用原来的简单等待方式
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
            # 如果出现异常，确保清理等待事件
            if staff_reply_manager and from_uid and event_id:
                try:
                    staff_reply_manager.stop_waiting(from_uid, event_id)
                except Exception:
                    pass
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

            # 处理channel可能是字符串或枚举对象的情况
            if channel is None:
                channel_str = "unknown"
            elif hasattr(channel, 'value'):
                channel_str = str(channel.value)
            else:
                channel_str = str(channel)

            return f"{channel_str}_{from_uid}"
        except Exception as e:
            self.logger.error(f"Failed to extract user ID: {e}")
            return "unknown_unknown"


# 全局实例
debounce_processor = DebounceProcessorAdapter()
