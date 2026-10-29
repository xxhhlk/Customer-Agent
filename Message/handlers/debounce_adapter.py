"""
防抖处理器适配器
为 EnhancedMessageConsumer 提供防抖等待与多消息合并
"""

import asyncio
from typing import Optional, Dict, Any
from bridge.context import Context
from utils.logger_loguru import get_logger
from ..models.queue_models import MessageWrapper


logger = get_logger(__name__)


class DebounceProcessorAdapter:
    """防抖处理器适配器 - 简化版"""

    # 防抖配置（白天/夜间时段由 config.business_hours 决定，非营业时间即夜间）
    DEBOUNCE_SECONDS = 8  # 白天防抖时间
    NIGHT_DEBOUNCE_SECONDS = 300  # 夜间防抖时间（5分钟）

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

    MAX_DEBOUNCE_DAY = 120  # 白天防抖总时长上限（秒）

    async def _wait_and_merge(
        self, 
        wrapper: MessageWrapper, 
        user_queue: Optional[asyncio.Queue],
        debounce_seconds: float,
        staff_reply_manager=None,
        from_uid=None,
        event_id=None
    ) -> Optional[MessageWrapper]:
        """等待防抖窗口并合并消息，同时监听人工回复事件。
        
        新消息到达会重置防抖计时器（标准防抖行为）。
        白天总防抖时长上限 MAX_DEBOUNCE_DAY 秒，夜间不设上限。
        """
        start_time = asyncio.get_event_loop().time()
        # 由秒数反推白天/夜间（故必须保证 NIGHT_DEBOUNCE_SECONDS > DEBOUNCE_SECONDS）；
        # 窗口中途不重查配置，避免上限判定在窗口内跳变
        is_daytime = debounce_seconds <= self.DEBOUNCE_SECONDS
        messages_to_merge = [wrapper]

        try:
            if not user_queue:
                # 没有队列，简单 sleep 后返回原消息
                await asyncio.sleep(debounce_seconds)
                return wrapper

            if staff_reply_manager and from_uid and event_id:
                # === 人工回复监听路径：同时等待队列消息 / 人工回复 / 超时 ===
                while True:
                    elapsed = asyncio.get_event_loop().time() - start_time

                    if is_daytime and elapsed >= self.MAX_DEBOUNCE_DAY:
                        self.logger.info(
                            f"白天防抖总时长达到 {self.MAX_DEBOUNCE_DAY}s 上限，强制结束"
                        )
                        break

                    wait_time = debounce_seconds
                    if is_daytime:
                        wait_time = min(debounce_seconds, self.MAX_DEBOUNCE_DAY - elapsed)

                    # 三个任务并行等待
                    queue_task = asyncio.create_task(user_queue.get())
                    staff_task = asyncio.create_task(
                        staff_reply_manager.wait_for_staff_reply(
                            from_uid, event_id, timeout=wait_time, auto_cleanup=False
                        )
                    )
                    timeout_task = asyncio.create_task(asyncio.sleep(wait_time))

                    done, pending = await asyncio.wait(
                        {queue_task, staff_task, timeout_task},
                        return_when=asyncio.FIRST_COMPLETED
                    )

                    # 取消未完成的任务
                    for task in pending:
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass

                    # 检查人工回复
                    if staff_task in done:
                        try:
                            staff_replied = staff_task.result()
                            if staff_replied:
                                self.logger.info(
                                    f"人工客服在防抖期间回复了 {from_uid}，取消自动回复"
                                )
                                staff_reply_manager.stop_waiting(from_uid, event_id)
                                user_key = self._extract_user_id(wrapper.context)
                                if user_key in self._last_message_time:
                                    del self._last_message_time[user_key]
                                # 已收集的其余消息并入首条：取消后它们不会被处理，
                                # 但内容仍需留给上层补写进会话历史
                                self._absorb_dropped(wrapper, messages_to_merge[1:])
                                return None
                        except Exception as e:
                            self.logger.error(f"检查人工回复结果时出错: {e}")

                    # 检查队列新消息 → 重置计时器
                    if queue_task in done:
                        try:
                            next_wrapper = queue_task.result()
                            messages_to_merge.append(next_wrapper)
                            self.logger.debug(
                                f"防抖重置: 收到新消息，已收集 {len(messages_to_merge)} 条"
                            )
                            continue
                        except Exception as e:
                            self.logger.error(f"获取队列消息时出错: {e}")

                    # timeout 完成：没有新消息也没有人工回复
                    break

            else:
                # === 简单路径：只等待队列新消息 ===
                while True:
                    elapsed = asyncio.get_event_loop().time() - start_time

                    if is_daytime and elapsed >= self.MAX_DEBOUNCE_DAY:
                        self.logger.info(
                            f"白天防抖总时长达到 {self.MAX_DEBOUNCE_DAY}s 上限，强制结束"
                        )
                        break

                    wait_time = debounce_seconds
                    if is_daytime:
                        wait_time = min(debounce_seconds, self.MAX_DEBOUNCE_DAY - elapsed)

                    try:
                        next_wrapper = await asyncio.wait_for(
                            user_queue.get(), timeout=wait_time
                        )
                        messages_to_merge.append(next_wrapper)
                        self.logger.debug(
                            f"防抖重置: 收到新消息，已收集 {len(messages_to_merge)} 条"
                        )
                    except asyncio.TimeoutError:
                        break

            # 兜底排空：防止 break 瞬间有新消息入队
            while True:
                try:
                    next_wrapper = user_queue.get_nowait()
                    messages_to_merge.append(next_wrapper)
                except asyncio.QueueEmpty:
                    break

            if len(messages_to_merge) == 1:
                return wrapper

            total_wait = asyncio.get_event_loop().time() - start_time
            self.logger.info(
                f"Merged {len(messages_to_merge)} messages (total wait: {total_wait:.1f}s)"
            )
            return self._merge_messages(messages_to_merge)

        except Exception as e:
            self.logger.error(f"Wait and merge error: {e}")
            if staff_reply_manager and from_uid and event_id:
                try:
                    staff_reply_manager.stop_waiting(from_uid, event_id)
                except Exception:
                    pass
            return wrapper

    @staticmethod
    def _context_line(context: Context) -> str:
        """把任意类型消息压成一行文本；非文本类型带类型标记，避免合并时静默丢失。"""
        content = str(context.content or "").strip()
        name = context.type.name
        if name == "TEXT":
            return content
        if name == "IMAGE":
            return f"[图片] {content}".rstrip()
        if name == "VIDEO":
            return f"[视频] {content}".rstrip()
        if name == "EMOTION":
            return content or "[表情]"
        return f"[{name}] {content}".rstrip() if content else f"[{name}]"

    # 需要保持消息类型（而非压成文本）的媒体类型：视觉模型靠它决定是否收图
    MEDIA_TYPES = ("IMAGE", "VIDEO")

    @staticmethod
    def _absorb_dropped(wrapper: MessageWrapper, dropped: list) -> None:
        """把因人工接管而不再处理的已收集消息并入首条。

        取消分支只把首条 wrapper 交给上层，其余消息若不并入就会随取消消失；
        它们承载的是人工回复前买家发出的内容，必须能在会话历史中留痕。
        """
        context = wrapper.context
        lines = [
            line
            for line in (
                DebounceProcessorAdapter._context_line(item.context) for item in dropped
            )
            if line
        ]
        if not lines:
            return
        if context.type.name == "TEXT":
            texts = [str(context.content)] if context.content else []
            context.content = "\n".join(texts + lines)
        else:
            # 首条是媒体：content 已被地址占用，文本改走 merged_text 留痕
            merged = str(getattr(context, "merged_text", None) or "").strip()
            context.merged_text = "\n".join(([merged] if merged else []) + lines)

    def _merge_messages(self, wrappers: list) -> MessageWrapper:
        """合并多条消息

        纯文本：全部内容按顺序拼进 content。
        含图片/视频：合并结果必须保留最近一条媒体消息的类型与地址，否则
        context 类型会被后续文本覆盖成 TEXT，AI 层不会把图交给视觉模型，
        模型只能看到一串 URL 文本（表现为回复「图片显示不出来」）。
        媒体 context 的 content 只能放地址，同批文本转存 merged_text。
        """
        last_wrapper = wrappers[-1]
        merged_context = last_wrapper.context

        # 文本行必须在改 merged_context 之前算：它本身就是最后一条 wrapper 的
        # context，改完类型后再按类型过滤会把这条的文本当成媒体内容吃掉。
        # 已经合并过的媒体 wrapper 带着 merged_text，二次合并（超时后补合并队列
        # 残留消息）时也要把它带下去，否则先前收集的文本会静默丢失。
        text_lines: list[str] = []
        media_count = 0
        for w in wrappers:
            if w.context.type.name in self.MEDIA_TYPES:
                media_count += 1
                prev_text = str(getattr(w.context, "merged_text", None) or "").strip()
                if prev_text:
                    text_lines.append(prev_text)
                continue
            line = self._context_line(w.context)
            if line:
                text_lines.append(line)

        media_wrapper = next(
            (
                w for w in reversed(wrappers)
                if w.context.type.name in self.MEDIA_TYPES and w.context.content
            ),
            None,
        )

        if media_wrapper is not None and media_wrapper is not last_wrapper:
            media_context = media_wrapper.context
            merged_context.type = media_context.type
            merged_context.content = media_context.content
            # 视频抽帧/封面依赖原始 info（时长、封面地址），一并带过来
            try:
                merged_context.kwargs.raw_data = media_context.kwargs.raw_data
            except Exception:
                pass

        if merged_context.type.name in self.MEDIA_TYPES:
            # 媒体地址已进 content，其余文本另存（媒体自身那行不入文本）
            merged_context.merged_text = "\n".join(text_lines) or None
            self.logger.info(
                f"合并保留媒体消息: type={merged_context.type.name}, "
                f"附带文本 {len(text_lines)} 行, 媒体 {media_count} 条（仅最近一条送入模型）"
            )
        elif merged_context.type.name == "TEXT":
            # 无可用媒体地址：退回纯文本合并，非文本类型降级为带标记的一行
            all_lines = [self._context_line(w.context) for w in wrappers]
            all_lines = [line for line in all_lines if line]
            if all_lines:
                merged_context.content = "\n".join(all_lines)

        return last_wrapper

    def _get_debounce_seconds(self) -> float:
        """获取当前应该使用的防抖等待时间

        营业时间内用白天防抖；非营业时间（夜间/凌晨）用夜间防抖，给买家更长的
        合并窗口。每条消息到达时判定一次，等待窗口内不重算。
        """
        from config import config
        if config.is_business_hours():
            return self.DEBOUNCE_SECONDS

        self.logger.info(f"非营业时间，使用夜间防抖 {self.NIGHT_DEBOUNCE_SECONDS}s")
        return self.NIGHT_DEBOUNCE_SECONDS

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
