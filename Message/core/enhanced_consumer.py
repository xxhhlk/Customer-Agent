"""
增强版消息消费者实现
集成防抖合并、AI超时中断重发、人工回复监听等功能
"""

import asyncio
import time
from typing import List, Dict, Any, Optional
from utils.logger_loguru import get_logger
from bridge.context import Context, ContextType
from .queue import queue_manager
from .handlers import MessageHandler, CatchAllHandler
from ..models.queue_models import MessageWrapper
from ..handlers.debounce_adapter import DebounceProcessorAdapter
from ..handlers.staff_reply_event import staff_reply_event_manager  # 使用全局单例
from ..handlers.rate_limiter import CozeRateLimiter


logger = get_logger(__name__)


class EnhancedMessageConsumer:
    """增强版消息消费者 - 集成防抖、AI超时等功能"""

    # 防抖配置
    DEBOUNCE_SECONDS = 8  # 白天防抖时间
    NIGHT_DEBOUNCE_SECONDS = 300  # 夜间防抖时间（5分钟）
    NIGHT_START = 23  # 夜间开始时间（23:01）
    NIGHT_END = 7   # 夜间结束时间（07:55）

    # AI超时配置
    CANCEL_WINDOW = 5  # AI取消窗口（秒）
    AI_TIMEOUT = 165  # AI总超时时间（秒）

    def __init__(self, queue_name: str, max_concurrent: int = 10):
        self.queue_name = queue_name
        self.max_concurrent = max_concurrent
        self.handlers: List[MessageHandler] = []
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.running = False
        self.consumer_task: Optional[asyncio.Task[None]] = None
        self.logger = get_logger(f"EnhancedConsumer.{queue_name}")

        # 用户队列管理（每个用户一个队列，保证顺序处理）
        self._user_queues: Dict[str, asyncio.Queue] = {}
        self._user_tasks: Dict[str, asyncio.Task] = {}

        # 防抖处理器
        self.debounce_processor = DebounceProcessorAdapter()

        # 人工回复事件管理器（使用全局单例）
        self.staff_reply_manager = staff_reply_event_manager

        # 限流器（使用全局单例）
        from Message.handlers.rate_limiter import coze_rate_limiter
        self.rate_limiter = coze_rate_limiter

    def add_handler(self, handler: MessageHandler):
        """添加处理器"""
        self.handlers.append(handler)
        self.logger.debug(f"Added handler: {handler.__class__.__name__}")

    def is_running(self) -> bool:
        """检查消费者是否正在运行"""
        return self.running

    async def start(self):
        """启动消费者"""
        if self.running:
            self.logger.warning(f"Consumer {self.queue_name} is already running")
            return

        self.running = True
        self.consumer_task = asyncio.create_task(self._consume_loop())
        self.logger.info(f"Enhanced Consumer {self.queue_name} started")

    async def _consume_loop(self):
        """消费循环"""
        queue = queue_manager.get_or_create_queue(self.queue_name)

        try:
            while self.running:
                try:
                    wrapper = await queue.get(timeout=1.0)
                    if wrapper:
                        # 提取用户ID，路由到用户队列
                        user_key = self._extract_user_id(wrapper.context)
                        await self._route_to_user_queue(user_key, wrapper)
                except Exception as e:
                    self.logger.error(f"Consumer error: {e}")
                    await asyncio.sleep(0.1)
        finally:
            self.logger.info(f"Enhanced Consumer {self.queue_name} stopped")

    async def _route_to_user_queue(self, user_key: str, wrapper: MessageWrapper):
        """路由消息到用户队列"""
        # 确保用户队列存在
        if user_key not in self._user_queues:
            self._user_queues[user_key] = asyncio.Queue()
            # 启动用户处理器任务
            self._user_tasks[user_key] = asyncio.create_task(
                self._process_user_queue(user_key)
            )
            self.logger.debug(f"Created user queue for {user_key}")

        # 放入用户队列
        await self._user_queues[user_key].put(wrapper)

    async def _process_user_queue(self, user_key: str):
        """处理用户队列（顺序处理）"""
        queue = self._user_queues[user_key]

        try:
            while self.running:
                try:
                    # 等待消息
                    wrapper = await asyncio.wait_for(queue.get(), timeout=1.0)

                    # 使用信号量控制全局并发
                    async with self.semaphore:
                        await self._process_message_with_debounce(wrapper)

                except asyncio.TimeoutError:
                    # 超时继续等待
                    continue
                except Exception as e:
                    self.logger.error(f"User queue {user_key} error: {e}")
                    await asyncio.sleep(0.1)
        finally:
            self.logger.debug(f"User queue {user_key} stopped")

    async def _process_message_with_debounce(self, wrapper: MessageWrapper):
        """带防抖的消息处理"""
        try:
            user_key = self._extract_user_id(wrapper.context)
            context = wrapper.context

            # 提前获取买家ID，用于人工回复监听
            from_uid = context.kwargs.from_uid if hasattr(context, 'kwargs') else None
            
            # 检查是否在冷却期内且有人在等待人工回复（跳过新消息，让它们合并到下一条）
            if from_uid and isinstance(from_uid, str):
                if self.staff_reply_manager.is_in_cooldown(from_uid) and self.staff_reply_manager.is_waiting(from_uid):
                    self.logger.info(f"User {user_key} in cooldown and has waiting event, skip this message (will merge to next)")
                    return
            
            # 检查是否需要监听人工回复（配置开启即可，不再限制时段）
            should_watch_staff_reply = False
            if from_uid and isinstance(from_uid, str):
                from config import get_config
                staff_wait_config = get_config("staff_reply_wait", {})
                enable_staff_wait = staff_wait_config.get("enable", True)
                if enable_staff_wait:
                    should_watch_staff_reply = True
            
            # 如果需要监听人工回复，在防抖等待前创建等待事件
            # 无论何时都创建等待事件（只要配置允许），确保在整个防抖期间都能监听人工客服回复
            event_id = None
            if should_watch_staff_reply and isinstance(from_uid, str):
                event_id = self.staff_reply_manager.start_waiting(from_uid)
                self.logger.debug(f"提前创建等待事件: {from_uid}, event_id={event_id}")

            try:
                # 1. 防抖合并（同时监听人工回复事件）
                merged_wrapper = await self.debounce_processor.process_with_debounce(
                    wrapper, 
                    self._user_queues.get(user_key),
                    self.staff_reply_manager if should_watch_staff_reply else None,
                    from_uid if isinstance(from_uid, str) else None,
                    event_id
                )

                if not merged_wrapper:
                    # 消息被合并，清理等待事件并返回
                    if event_id and isinstance(from_uid, str):
                        self.staff_reply_manager.stop_waiting(from_uid, event_id)
                    return

                # 2. 检查人工回复（使用提前创建的等待事件）
                if event_id and isinstance(from_uid, str):
                    staff_replied = await self._check_staff_reply_with_event(
                        merged_wrapper.context, 
                        from_uid, 
                        event_id
                    )
                    if staff_replied:
                        self.logger.info(f"User {user_key} staff replied, skip AI")
                        return
                    else:
                        # 等待超时，检查队列中是否有冷却期内被跳过的消息
                        user_queue = self._user_queues.get(user_key)
                        if user_queue:
                            pending_messages = []
                            while True:
                                try:
                                    pending_wrapper = user_queue.get_nowait()
                                    pending_messages.append(pending_wrapper)
                                except asyncio.QueueEmpty:
                                    break
                            
                            if pending_messages:
                                self.logger.info(f"Found {len(pending_messages)} pending messages, merging...")
                                # 合并消息
                                all_messages = [merged_wrapper] + pending_messages
                                merged_wrapper = self._merge_messages(all_messages)
                else:
                    # 配置关闭，直接处理
                    staff_replied = await self._check_staff_reply(merged_wrapper.context)
                    if staff_replied:
                        self.logger.info(f"User {user_key} staff replied, skip AI")
                        return

                # 3. 处理消息（带AI超时中断）
                await self._process_message_with_ai_timeout(merged_wrapper)
            finally:
                # 清理等待事件
                if event_id and isinstance(from_uid, str):
                    self.staff_reply_manager.stop_waiting(from_uid, event_id)

        except Exception as e:
            self.logger.error(f"Failed to process message with debounce: {e}")

    async def _check_staff_reply(self, context: Context) -> bool:
        """检查人工客服是否已回复"""
        # 检查配置
        from config import get_config
        staff_wait_config = get_config("staff_reply_wait", {})
        enable_staff_wait = staff_wait_config.get("enable", True)
        wait_seconds = staff_wait_config.get("wait_seconds", 30)

        if not enable_staff_wait:
            return False

        from_uid = context.kwargs.from_uid if hasattr(context, 'kwargs') else None
        if not from_uid:
            return False

        # 夜间时段使用更长的等待时间
        current_hour = time.localtime().tm_hour
        is_night = current_hour >= self.NIGHT_START or current_hour <= self.NIGHT_END
        if is_night:
            wait_seconds = max(wait_seconds, 60)  # 夜间至少等待60秒

        self.logger.info(f"Waiting for staff reply (max {wait_seconds}s)")

        # 开始等待
        event_id = self.staff_reply_manager.start_waiting(from_uid)
        try:
            staff_replied = await self.staff_reply_manager.wait_for_staff_reply(
                from_uid, 
                event_id,
                timeout=wait_seconds
            )
            return staff_replied
        finally:
            self.staff_reply_manager.stop_waiting(from_uid, event_id)

    async def _check_staff_reply_with_event(
        self, 
        context: Context, 
        from_uid: str, 
        event_id: str
    ) -> bool:
        """检查人工客服是否已回复（使用提前创建的等待事件）"""
        # 检查配置
        from config import get_config
        staff_wait_config = get_config("staff_reply_wait", {})
        wait_seconds = staff_wait_config.get("wait_seconds", 30)
        
        # 检查是否在冷却期内，如果是则延长等待时间
        extended_wait = self.staff_reply_manager.get_extended_wait_time(from_uid)
        if extended_wait > 0:
            wait_seconds = extended_wait
            self.logger.info(f"User in cooldown, extended wait to {wait_seconds}s")

        self.logger.info(f"Waiting for staff reply (max {wait_seconds}s, event_id={event_id})")

        try:
            # 不自动清理，由外层的 finally 块负责清理
            staff_replied = await self.staff_reply_manager.wait_for_staff_reply(
                from_uid, 
                event_id,
                timeout=wait_seconds,
                auto_cleanup=False  # 关键：不自动清理
            )
            return staff_replied
        finally:
            # 注意：这里不清理事件，由调用者清理
            pass

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

    async def _process_message_with_ai_timeout(self, wrapper: MessageWrapper):
        """带AI超时中断的消息处理"""
        user_key = self._extract_user_id(wrapper.context)
        context = wrapper.context

        # 构建 metadata，补充渠道上下文
        metadata = wrapper.to_metadata()
        try:
            kwargs = getattr(context, 'kwargs', None)
            if kwargs:
                metadata['shop_id'] = getattr(kwargs, 'shop_id', None)
                metadata['user_id'] = getattr(kwargs, 'user_id', None)
                metadata['from_uid'] = getattr(kwargs, 'from_uid', None)
        except Exception:
            pass
        metadata['user_key'] = user_key

        # 限流检查 - 在处理AI请求之前检查用户是否超出限流阈值
        from_uid = metadata.get('from_uid')
        if from_uid:
            if self.rate_limiter.is_rate_limited(from_uid):
                self.logger.warning(f"用户 {from_uid} 已超出限流阈值，使用兜底回复")
                # 使用兜底回复
                await self._send_fallback_reply(context, metadata)
                return

        # 先尝试非AI处理器（关键词、转人工等）
        ai_handler = None
        catch_all_handler = None

        for handler in self.handlers:
            # 检查是否是AI处理器
            is_ai_handler = hasattr(handler, '_get_ai_reply')
            # 检查是否是CatchAllHandler
            is_catch_all = isinstance(handler, CatchAllHandler)
            
            if handler.can_handle(context):
                if is_ai_handler:
                    # 记录AI处理器，稍后处理
                    ai_handler = handler
                elif is_catch_all:
                    # 记录CatchAllHandler，作为最后的兜底
                    catch_all_handler = handler
                else:
                    # 其他非AI处理器，立即执行
                    try:
                        success = await handler.handle(context, metadata)
                        if success:
                            self.logger.debug(f"Message handled by {handler.__class__.__name__}")
                            return  # 处理成功，直接返回
                        else:
                            self.logger.debug(f"Handler {handler.__class__.__name__} returned False, continuing...")
                    except Exception as e:
                        self.logger.error(f"Handler {handler.__class__.__name__} error: {e}")

        # 如果没有AI处理器，执行CatchAllHandler
        if not ai_handler:
            if catch_all_handler:
                await catch_all_handler.handle(context, metadata)
            return

        # 执行AI处理器（带超时中断机制）
        ai_start_time = time.time()
        from_uid_raw = context.kwargs.from_uid if hasattr(context, 'kwargs') else None
        from_uid = from_uid_raw if from_uid_raw else "unknown"

        # 创建任务
        ai_task = asyncio.create_task(ai_handler.handle(context, metadata))
        queue_task = asyncio.create_task(self._user_queues[user_key].get())

        # 人工回复监听任务
        staff_reply_event_id = self.staff_reply_manager.start_waiting(from_uid)
        staff_reply_task = asyncio.create_task(
            self.staff_reply_manager.wait_for_staff_reply(from_uid, staff_reply_event_id, timeout=300)
        )

        try:
            while True:
                done, pending = await asyncio.wait(
                    {ai_task, queue_task, staff_reply_task},
                    return_when=asyncio.FIRST_COMPLETED
                )

                if ai_task in done:
                    # AI完成
                    queue_task.cancel()
                    staff_reply_task.cancel()
                    try:
                        await queue_task
                        await staff_reply_task
                    except asyncio.CancelledError:
                        pass
                    break

                elif queue_task in done:
                    # 新消息到达
                    staff_reply_task.cancel()
                    try:
                        await staff_reply_task
                    except asyncio.CancelledError:
                        pass

                    new_msg = queue_task.result()
                    elapsed = time.time() - ai_start_time

                    if elapsed < self.CANCEL_WINDOW:
                        # 在取消窗口内 → 取消AI，处理新消息
                        ai_task.cancel()
                        try:
                            await ai_task
                        except (asyncio.CancelledError, Exception) as e:
                            if not isinstance(e, asyncio.CancelledError):
                                self.logger.warning(f"AI task error on cancel: {e}")

                        self.logger.info(f"AI timeout ({elapsed:.1f}s), new message arrived")

                        # 新消息放回队列
                        await self._user_queues[user_key].put(new_msg)
                        return
                    else:
                        # 超出取消窗口 → 新消息放回队列，继续等AI
                        self.logger.debug(f"AI processing {elapsed:.1f}s, wait for completion")
                        await self._user_queues[user_key].put(new_msg)
                        queue_task = asyncio.create_task(self._user_queues[user_key].get())

                elif staff_reply_task in done:
                    # 人工客服回复了
                    ai_task.cancel()
                    queue_task.cancel()
                    try:
                        await ai_task
                        await queue_task
                    except asyncio.CancelledError:
                        pass

                    self.logger.info(f"Staff replied during AI processing, cancel AI")
                    return

        except Exception as e:
            ai_task.cancel()
            queue_task.cancel()
            self.logger.error(f"AI timeout processing error: {e}")
            raise
        finally:
            self.staff_reply_manager.stop_waiting(from_uid, staff_reply_event_id)

    async def _process_message_normal(self, wrapper: MessageWrapper):
        """正常消息处理（无AI超时）"""
        try:
            processed = False
            metadata = wrapper.to_metadata()

            # 追加渠道上下文到metadata
            try:
                kwargs = getattr(wrapper.context, 'kwargs', None)
                if kwargs:
                    metadata['shop_id'] = getattr(kwargs, 'shop_id', None)
                    metadata['user_id'] = getattr(kwargs, 'user_id', None)
                    metadata['from_uid'] = getattr(kwargs, 'from_uid', None)
            except Exception:
                pass

            metadata['user_key'] = self._extract_user_id(wrapper.context)

            # 调用处理器链
            for handler in self.handlers:
                try:
                    if handler.can_handle(wrapper.context):
                        success = await handler.handle(wrapper.context, metadata)
                        if success:
                            processed = True
                            self.logger.debug(f"Message {wrapper.message_id} handled by {handler.__class__.__name__}")
                            break
                except Exception as e:
                    self.logger.error(f"Handler {handler.__class__.__name__} error: {e}")
                    continue

            if not processed:
                self.logger.warning(f"Message {wrapper.message_id} not processed by any handler")

        except Exception as e:
            self.logger.error(f"Failed to process message {wrapper.message_id}: {e}")

    async def stop(self):
        """停止消费者"""
        self.running = False

        # 取消所有用户任务
        for task in self._user_tasks.values():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # 取消消费任务
        if self.consumer_task is not None:
            self.consumer_task.cancel()
            try:
                await self.consumer_task
            except asyncio.CancelledError:
                pass

        # 等待所有正在处理的任务完成
        await self.semaphore.acquire()
        self.semaphore.release()

        self.logger.info(f"Enhanced Consumer {self.queue_name} stopped")

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

    async def _send_fallback_reply(self, context: Context, metadata: Dict[str, Any]):
        """发送兜底回复"""
        try:
            # 获取兜底回复配置
            from config import get_config
            rate_limit_config = get_config("rate_limit", {})
            fallback_replies = rate_limit_config.get("fallback_reply", [])
            
            # 如果没有配置兜底回复，使用默认回复
            if not fallback_replies:
                fallback_replies = ["亲，感谢您的咨询！客服正在为您处理，请稍等片刻。"]
            
            # 随机选择一个兜底回复
            import random
            reply_text = random.choice(fallback_replies)
            
            # 发送回复
            shop_id = metadata.get('shop_id')
            user_id = metadata.get('user_id')
            from_uid = metadata.get('from_uid')

            if not shop_id or not user_id or not from_uid:
                self.logger.warning(f"缺少发送信息: shop_id={shop_id}, user_id={user_id}, from_uid={from_uid}")
                return

            # 尝试发送消息
            from Channel.pinduoduo.utils.API.send_message import SendMessage
            sender = SendMessage(str(shop_id), str(user_id))
            result = sender.send_text(str(from_uid), reply_text)
            if isinstance(result, dict) and result.get("success"):
                self.logger.info(f"已发送兜底回复给用户 {from_uid}")
            else:
                self.logger.warning(f"发送兜底回复失败: {result}")
        except Exception as e:
            self.logger.error(f"发送兜底回复时出错: {e}")


class EnhancedMessageConsumerManager:
    """增强版消息消费者管理器"""

    def __init__(self):
        self._consumers: Dict[str, EnhancedMessageConsumer] = {}
        self.logger = get_logger("EnhancedConsumerManager")

    def create_consumer(self, queue_name: str, max_concurrent: int = 10) -> EnhancedMessageConsumer:
        """创建消费者"""
        if queue_name in self._consumers:
            self.logger.warning(f"Consumer {queue_name} already exists")
            return self._consumers[queue_name]

        consumer = EnhancedMessageConsumer(queue_name, max_concurrent)
        self._consumers[queue_name] = consumer
        self.logger.info(f"Created enhanced consumer: {queue_name}")
        return consumer

    def get_consumer(self, queue_name: str) -> Optional[EnhancedMessageConsumer]:
        """获取消费者"""
        return self._consumers.get(queue_name)

    async def start_consumer(self, queue_name: str):
        """启动消费者"""
        consumer = self.get_consumer(queue_name)
        if consumer:
            await consumer.start()
        else:
            self.logger.error(f"Consumer {queue_name} not found")

    async def stop_consumer(self, queue_name: str):
        """停止消费者"""
        consumer = self.get_consumer(queue_name)
        if consumer:
            await consumer.stop()
        else:
            self.logger.error(f"Consumer {queue_name} not found")

    def list_consumers(self) -> List[str]:
        """列出所有消费者"""
        return list(self._consumers.keys())

    async def stop_all(self):
        """停止所有消费者"""
        for consumer in self._consumers.values():
            await consumer.stop()
        self.logger.info("All enhanced consumers stopped")


# 全局增强版消费者管理器实例
enhanced_message_consumer_manager = EnhancedMessageConsumerManager()
