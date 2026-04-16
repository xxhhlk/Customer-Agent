"""
消息消费者系统
用于处理消息队列中的Context格式消息
"""

import time

import asyncio
from typing import Callable, Optional, Dict, Any, List, Awaitable
from abc import ABC, abstractmethod
from bridge.context import Context, ContextType
from Message.message_queue import message_queue_manager
from utils.logger import get_logger

logger = get_logger()

class MessageHandler(ABC):
    """消息处理器抽象基类"""
    
    @abstractmethod
    async def handle(self, context: Context, metadata: Dict[str, Any]) -> bool:
        """
        处理消息
        
        Args:
            context: Context格式的消息
            metadata: 消息元数据（包含ID、时间戳等）
            
        Returns:
            bool: 是否处理成功
        """
        pass
    
    @abstractmethod
    def can_handle(self, context: Context) -> bool:
        """
        判断是否能处理该消息
        
        Args:
            context: Context格式的消息
            
        Returns:
            bool: 是否能处理
        """
        pass


class TypeBasedHandler(MessageHandler):
    """基于消息类型的处理器"""
    
    def __init__(self, supported_types: set, handler_func: Callable[[Context, Dict[str, Any]], Awaitable[bool]]):
        """
        初始化类型处理器
        
        Args:
            supported_types: 支持的消息类型集合
            handler_func: 处理函数
        """
        self.supported_types = supported_types
        self.handler_func = handler_func
    
    def can_handle(self, context: Context) -> bool:
        """检查是否支持该消息类型"""
        return context.type in self.supported_types
    
    async def handle(self, context: Context, metadata: Dict[str, Any]) -> bool:
        """调用处理函数处理消息"""
        try:
            return await self.handler_func(context, metadata)
        except Exception as e:
            logger.error(f"消息处理失败: {e}")
            return False


class ChannelBasedHandler(MessageHandler):
    """基于渠道类型的处理器"""
    
    def __init__(self, supported_channels: set, handler_func: Callable[[Context, Dict[str, Any]], Awaitable[bool]]):
        """
        初始化渠道处理器
        
        Args:
            supported_channels: 支持的渠道类型集合
            handler_func: 处理函数
        """
        self.supported_channels = supported_channels
        self.handler_func = handler_func
    
    def can_handle(self, context: Context) -> bool:
        """检查是否支持该渠道类型"""
        return context.channel_type in self.supported_channels
    
    async def handle(self, context: Context, metadata: Dict[str, Any]) -> bool:
        """调用处理函数处理消息"""
        try:
            return await self.handler_func(context, metadata)
        except Exception as e:
            logger.error(f"消息处理失败: {e}")
            return False


class UserSequentialProcessor:
    """用户消息顺序处理器 - 支持防抖合并 + AI超时中断重发 + 夜间延迟"""

    # 防抖等待时间（秒）
    DEBOUNCE_SECONDS = 5
    # 夜间防抖等待时间（秒）- 5分钟
    NIGHT_DEBOUNCE_SECONDS = 300
    # 夜间时段配置（小时:分钟）
    NIGHT_START = (23, 1)   # 23:01
    NIGHT_END = (7, 55)     # 07:55
    # AI超时中断窗口（秒）：AI回复超过此时间后，如果客户又发消息，则取消当前请求
    CANCEL_WINDOW = 25.0

    def __init__(self, user_id: str, handlers: List[MessageHandler]):
        self.user_id = user_id
        self.handlers = handlers
        self._message_queue = None  # 惰性初始化，绑定到实际使用的 event loop
        self.is_processing = False
        self.processor_task = None
        self.logger = get_logger()
        # AI pending状态
        self._ai_pending = None  # Dict: {chat_info, original_content, original_kwargs, can_cancel, start_time}

    @property
    def message_queue(self) -> asyncio.Queue:
        """惰性获取 message_queue，确保绑定到当前 event loop"""
        if self._message_queue is None:
            self._message_queue = asyncio.Queue()
        return self._message_queue

    def _is_night_time(self) -> bool:
        """
        判断当前是否在夜间时段（23:01 - 次日07:55）

        Returns:
            bool: 是否在夜间时段
        """
        from datetime import datetime

        now = datetime.now()
        current_hour = now.hour
        current_minute = now.minute

        # 转换为分钟数便于比较
        current_minutes = current_hour * 60 + current_minute
        night_start_minutes = self.NIGHT_START[0] * 60 + self.NIGHT_START[1]  # 23:01 = 1381
        night_end_minutes = self.NIGHT_END[0] * 60 + self.NIGHT_END[1]        # 07:55 = 475

        # 夜间时段跨天：从23:01到次日07:55
        # 如果当前时间 >= 23:01 或者 < 07:55，则在夜间时段
        if current_minutes >= night_start_minutes:
            # 当天23:01之后
            return True
        elif current_minutes < night_end_minutes:
            # 次日07:55之前
            return True
        else:
            # 白天时段
            return False

    def _get_debounce_seconds(self) -> float:
        """
        根据当前时段获取防抖等待时间

        Returns:
            float: 防抖等待时间（秒）
        """
        if self._is_night_time():
            self.logger.debug(f"用户 {self.user_id} 夜间时段，使用{self.NIGHT_DEBOUNCE_SECONDS}秒防抖")
            return self.NIGHT_DEBOUNCE_SECONDS
        else:
            return self.DEBOUNCE_SECONDS
    
    async def add_message(self, message_wrapper: Dict[str, Any]):
        """添加消息到用户队列，先校验消息类型，过滤无效消息"""
        if not isinstance(message_wrapper, dict):
            self.logger.debug(f"过滤无效消息，类型：{type(message_wrapper)}，值：{message_wrapper}")
            return
        await self.message_queue.put(message_wrapper)

        # 启动处理器（如果未运行）
        if not self.is_processing:
            self.processor_task = asyncio.create_task(self._process_user_messages())

    async def _process_user_messages(self):
        """处理用户消息队列（防抖合并处理 + AI超时中断重发）"""
        self.is_processing = True
        try:
            while True:
                try:
                    # 检查是否有待处理的重发（_process_single_message 设置的）
                    if self._ai_pending:
                        await self._do_cancel_resend()
                        continue

                    # 正常：等待第一条消息，超时后退出
                    first_message = await asyncio.wait_for(
                        self.message_queue.get(),
                        timeout=30.0
                    )

                    # 启动防抖收集
                    buffered_messages = [first_message]
                    self.logger.debug(f"用户 {self.user_id} 创建buffered_messages, 长度={len(buffered_messages)}, id={id(buffered_messages)}")
                    await self._collect_with_debounce(buffered_messages)
                    self.logger.debug(f"用户 {self.user_id} 防抖收集后buffered_messages长度={len(buffered_messages)}, id={id(buffered_messages)}")

                    # 处理收集到的消息
                    await self._process_batch(buffered_messages)

                except asyncio.TimeoutError:
                    self.logger.debug(f"用户 {self.user_id} 消息处理器超时退出")
                    break

        except Exception as e:
            self.logger.error(f"用户 {self.user_id} 消息处理器异常: {e}")
        finally:
            self.is_processing = False
    
    async def _collect_with_debounce(self, buffered: list):
        """防抖收集：每次收到新消息刷新倒计时，超时后返回"""
        debounce_seconds = self._get_debounce_seconds()
        is_night = self._is_night_time()

        if is_night:
            self.logger.info(f"用户 {self.user_id} 夜间时段，开始{debounce_seconds}秒防抖收集")

        self.logger.debug(f"用户 {self.user_id} 开始防抖收集, buffered初始长度={len(buffered)}, id={id(buffered)}")

        # 防抖期人工回复监听
        from Message.staff_reply_event import staff_reply_event_manager
        staff_replied = False
        staff_reply_task = None
        staff_reply_event_id = None
        from_uid = ""
        # 从第一条消息获取from_uid
        if buffered:
            first_context = buffered[0]['context']
            from_uid = first_context.kwargs.get('from_uid', '')
            if from_uid:
                # 先清理之前可能存在的未结束监听，避免重复start导致stop时找不到记录
                try:
                    staff_reply_event_manager.stop_waiting(from_uid, None)
                except Exception as e:
                    self.logger.debug(f"清理旧人工监听异常：{e}")
                # 启动人工回复监听
                staff_reply_event_id = staff_reply_event_manager.start_waiting(from_uid)
                staff_reply_task = asyncio.create_task(
                    staff_reply_event_manager.wait_for_staff_reply(from_uid, staff_reply_event_id, timeout=debounce_seconds)
                )
        try:
            while True:
                # 构建等待任务列表，必须包装为Task对象，不能直接传协程
                queue_wait_task = asyncio.create_task(asyncio.wait_for(self.message_queue.get(), timeout=debounce_seconds))
                wait_tasks = [queue_wait_task]
                if staff_reply_task and not staff_reply_task.done():
                    wait_tasks.append(staff_reply_task)
                
                # 等待第一个完成的任务
                done, _ = await asyncio.wait(wait_tasks, return_when=asyncio.FIRST_COMPLETED)
                
                # 检查人工回复任务
                if staff_reply_task in done:
                    # 收到人工回复，停止收集
                    staff_replied = staff_reply_task.result()
                    if staff_replied:
                        self.logger.info(f"用户 {self.user_id} 防抖期收到人工客服回复，停止消息收集，丢弃{len(buffered)}条消息")
                        buffered.clear()  # 清空消息，后续不再处理
                        break
                    else:
                        # 人工回复超时（返回False），从done中移除，继续检查队列任务
                        done.discard(staff_reply_task)
                
                # 检查是否有队列消息任务完成
                if queue_wait_task not in done:
                    # 没有新消息，继续循环等待
                    continue
                
                # 处理新消息
                try:
                    message = queue_wait_task.result()
                    # 添加类型检查日志
                    if not isinstance(message, dict):
                        self.logger.warning(f"用户 {self.user_id} 收到非dict类型消息: {type(message)}, value={message}")
                    self.logger.debug(f"用户 {self.user_id} 收到新消息, buffered当前长度={len(buffered)}, 准备追加")
                    buffered.append(message)
                    self.logger.debug(f"用户 {self.user_id} 追加后buffered长度={len(buffered)}")
                    # 夜间时段每次收到新消息都刷新倒计时
                    if is_night:
                        self.logger.info(f"用户 {self.user_id} 夜间时段收到新消息，重置{debounce_seconds}秒倒计时")
                    # 重新创建监听任务（因为超时时间重置了）
                    if staff_reply_event_id:
                        staff_reply_task.cancel()
                        try:
                            await staff_reply_task
                        except asyncio.CancelledError:
                            pass
                        staff_reply_task = asyncio.create_task(
                            staff_reply_event_manager.wait_for_staff_reply(from_uid, staff_reply_event_id, timeout=debounce_seconds)
                        )
                    # 继续循环，重新等待
                except asyncio.TimeoutError:
                    # 超时无新消息，收集结束
                    self.logger.debug(f"用户 {self.user_id} 防抖超时, buffered最终长度={len(buffered)}, id={id(buffered)}")
                    if len(buffered) > 1:
                        self.logger.info(f"用户 {self.user_id} 防抖收集完成，合并 {len(buffered)} 条消息")
                    if is_night:
                        self.logger.info(f"用户 {self.user_id} 夜间防抖结束，准备发送合并消息")
                    break
        finally:
            # 清理人工等待状态
            if staff_reply_event_id and from_uid:
                try:
                    staff_reply_event_manager.stop_waiting(from_uid, staff_reply_event_id)
                    if staff_reply_task:
                        staff_reply_task.cancel()
                        try:
                            await staff_reply_task
                        except asyncio.CancelledError:
                            pass
                except Exception as e:
                    self.logger.debug(f"清理人工监听异常：{e}")

    async def _process_batch(self, messages: list):
        """处理一批消息（可能是合并后的）"""
        # 空列表直接返回
        if not messages:
            self.logger.debug(f"用户 {self.user_id} _process_batch: 消息列表为空，跳过处理")
            return

        # 添加调试日志，记录消息数量和类型
        self.logger.debug(f"用户 {self.user_id} _process_batch: messages类型={type(messages)}, len={len(messages)}")
        if messages:
            self.logger.debug(f"用户 {self.user_id} 第一条消息类型={type(messages[0])}, keys={list(messages[0].keys()) if isinstance(messages[0], dict) else 'N/A'}")

        # 检查是否只有图片/视频消息（没有文本）
        has_text = False
        has_media = False
        for msg in messages:
            ctx = msg.get('context')
            if ctx:
                if ctx.type == ContextType.TEXT:
                    has_text = True
                    break
                elif ctx.type in (ContextType.IMAGE, ContextType.VIDEO):
                    has_media = True

        # 如果只有图片/视频没有文本，直接回复提示语
        if has_media and not has_text:
            await self._handle_media_only(messages)
            return

        if len(messages) == 1:
            # 单条消息，直接处理
            await self._process_single_message(messages[0])
        else:
            # 多条消息，合并后处理
            await self._process_merged_message(messages)
    
    async def _process_merged_message(self, messages: list):
        """合并多条消息为一条后处理"""
        import json
        
        first_context = messages[0]['context']
        merged_parts = []
        
        for msg_wrapper in messages:
            ctx = msg_wrapper['context']
            part = self._context_to_text(ctx)
            if part:
                merged_parts.append(part)
        
        merged_text = "\n".join(merged_parts)
        
        self.logger.info(f"用户 {self.user_id} 合并 {len(messages)} 条消息: {merged_text[:200]}...")
        
        # 创建合并后的 context
        merged_context = Context(
            type=ContextType.TEXT,
            content=merged_text,
            channel_type=first_context.channel_type,
            kwargs=first_context.kwargs
        )
        
        # 使用最后一条消息的 wrapper 作为 metadata（保留最新上下文）
        merged_wrapper = messages[-1].copy()
        merged_wrapper['context'] = merged_context
        merged_wrapper['merged_count'] = len(messages)
        
        await self._process_single_message(merged_wrapper)

    async def _handle_media_only(self, messages: list):
        """
        处理纯图片/视频消息（没有文本）
        直接发送提示语，不走AI流程
        """
        try:
            # 从第一条消息获取必要信息
            first_msg = messages[0]
            context = first_msg.get('context')
            if not context:
                return

            shop_id = context.kwargs.get('shop_id')
            user_id = context.kwargs.get('user_id')
            from_uid = context.kwargs.get('from_uid')
            username = context.kwargs.get('username', '')
            nickname = context.kwargs.get('nickname', '')

            if not all([shop_id, user_id, from_uid]):
                self.logger.warning("纯媒体消息缺少必要信息，跳过处理")
                return

            # 统计消息类型
            media_types = []
            for msg in messages:
                ctx = msg.get('context')
                if ctx:
                    if ctx.type == ContextType.IMAGE:
                        media_types.append('图片')
                    elif ctx.type == ContextType.VIDEO:
                        media_types.append('视频')

            self.logger.info(
                f"[{username}] 用户[{nickname}]({from_uid}) 发送纯媒体消息: {', '.join(media_types)}"
            )

            # 发送提示语
            from Channel.pinduoduo.utils.API.send_message import SendMessage
            sender = SendMessage(shop_id, user_id)
            reply_text = "请问您具体想问什么问题呢？"
            sender.send_text(from_uid, reply_text)
            self.logger.info(f"[{username}] 已向用户[{nickname}]发送媒体消息提示")

        except Exception as e:
            self.logger.error(f"处理纯媒体消息失败: {e}")

    def _context_to_text(self, context: Context) -> str:
        """将不同类型的 context 转换为文本，保留关键信息"""
        try:
            if context.type == ContextType.TEXT:
                return context.content or ""
            
            elif context.type == ContextType.IMAGE:
                # 保留图片URL
                url = context.content if context.content else ""
                return f"[图片] {url}" if url else "[图片]"
            
            elif context.type == ContextType.VIDEO:
                # 保留视频URL
                url = context.content if context.content else ""
                return f"[视频] {url}" if url else "[视频]"
            
            elif context.type == ContextType.EMOTION:
                # 保留表情描述
                desc = context.content if context.content else ""
                return f"[表情] {desc}" if desc else "[表情]"
            
            elif context.type == ContextType.GOODS_INQUIRY or context.type == ContextType.GOODS_SPEC:
                info = context.content if isinstance(context.content, dict) else {}
                name = info.get('goods_name', '')
                price = info.get('goods_price', '')
                spec = info.get('goods_spec', '')
                link = info.get('link_url', '')
                parts = []
                if name:
                    parts.append(name)
                if price:
                    parts.append(f"¥{price}")
                if spec:
                    parts.append(f"规格: {spec}")
                if link:
                    parts.append(link)
                label = "[商品咨询]" if context.type == ContextType.GOODS_INQUIRY else "[商品规格]"
                detail = " ".join(parts) if parts else ""
                return f"{label} {detail}" if detail else label
            
            elif context.type == ContextType.ORDER_INFO:
                info = context.content if isinstance(context.content, dict) else {}
                order_id = info.get('order_id', '')
                goods_name = info.get('goods_name', '')
                spec = info.get('spec', '')
                parts = []
                if order_id:
                    parts.append(f"订单号: {order_id}")
                if goods_name:
                    parts.append(f"商品: {goods_name}")
                if spec:
                    parts.append(f"规格: {spec}")
                return f"[订单] {' | '.join(parts)}" if parts else "[订单]"
            
            else:
                return str(context.content or "")
        except Exception as e:
            self.logger.error(f"转换context到文本失败: {e}")
            return ""
    
    async def _check_staff_reply(self, context) -> tuple[bool, float]:
        """
        检查人工客服是否已回复
        
        Args:
            context: 消息上下文
            
        Returns:
            tuple[bool, float]: (人工是否已回复, 等待耗时秒数)
        """
        from config import config
        from Message.staff_reply_event import staff_reply_event_manager
        
        staff_wait_config = config.get_staff_reply_wait_config()
        enable_staff_wait = staff_wait_config['enable']
        wait_seconds = staff_wait_config['wait_seconds']
        
        if not enable_staff_wait:
            return False, 0.0
        
        from_uid = context.kwargs.get('from_uid', '')
        if not from_uid:
            return False, 0.0
        
        self.logger.info(
            f"用户 {self.user_id} 等待人工客服回复 "
            f"(最多{wait_seconds}秒)"
        )

        # 开始等待
        event_id = staff_reply_event_manager.start_waiting(from_uid)
        staff_wait_elapsed = 0.0
        
        try:
            staff_replied = await staff_reply_event_manager.wait_for_staff_reply(
                from_uid, 
                event_id,
                timeout=wait_seconds
            )
            
            if staff_replied:
                # 人工客服已回复
                self.logger.info(
                    f"用户 {self.user_id} 人工客服已回复，跳过AI处理"
                )
                return True, 0.0
            else:
                # 超时，继续AI处理
                staff_wait_elapsed = wait_seconds
                self.logger.info(
                    f"用户 {self.user_id} 等待人工客服超时({wait_seconds}秒)，"
                    f"继续AI处理"
                )
                return False, staff_wait_elapsed
        finally:
            # 确保清理等待状态
            staff_reply_event_manager.stop_waiting(from_uid, event_id)

    async def _process_single_message(self, message_wrapper: Dict[str, Any]):
        """
        处理单条消息 - 并行执行AI处理和新消息监听

        流程：
        1. 查找handler
        2. 非AI处理器：直接handle
        3. AI处理器：
           a. 白天时段 + 启用人工等待 → 等待人工客服回复
           b. 人工回复了 → 直接返回，不调用AI
           c. 超时/未启用 → 并行运行 handler.handle() + message_queue监听
              - AI先完成 → 正常结束
              - 新消息先到 + <25s → 取消AI，收集新消息，设置_ai_pending，return
              - 新消息先到 + >25s → 放回队列，继续等AI
        """
        message_id = message_wrapper['id']
        context = message_wrapper['context']
        
        # 统一打印收到的消息日志
        username = context.kwargs.get("username", "")
        nickname = context.kwargs.get("nickname", "")
        from_uid = context.kwargs.get("from_uid", "")
        self.logger.info(f"[{username}]收到用户[{nickname}]({from_uid})消息: 类型={context.type.name}, 内容={self._context_to_text(context)}")

        try:
            # 查找处理器
            handler = None
            for h in self.handlers:
                if h.can_handle(context):
                    handler = h
                    break

            if not handler:
                self.logger.warning(f"用户 {self.user_id} 消息 {message_id} 没有合适的处理器")
                return

            self.logger.debug(f"用户 {self.user_id} 使用处理器 {handler.__class__.__name__} 处理消息 {message_id}")

            # 非AI处理器：直接处理
            if not hasattr(handler, '_get_ai_reply'):
                success = await handler.handle(context, message_wrapper)
                if not success:
                    # 返回False可能表示"还有剩余内容需要下一个处理器处理"
                    # 继续查找下一个处理器（比如AI处理器）
                    self.logger.debug(f"用户 {self.user_id} 处理器 {handler.__class__.__name__} 返回False，继续查找下一个处理器")
                    # 从当前处理器之后继续查找
                    handler_index = self.handlers.index(handler)
                    for next_handler in self.handlers[handler_index + 1:]:
                        if next_handler.can_handle(context):
                            self.logger.debug(f"用户 {self.user_id} 找到下一个处理器 {next_handler.__class__.__name__}")
                            handler = next_handler
                            break
                    else:
                        # 没有下一个处理器了
                        self.logger.warning(f"用户 {self.user_id} 消息 {message_id} 没有更多处理器")
                        return
                else:
                    # 处理成功，直接返回
                    return

            # AI处理器：检查是否需要等待人工客服回复
            staff_replied, staff_wait_elapsed = await self._check_staff_reply(context)
            if staff_replied:
                return
            
            # 将等待时间传递给AI处理器（用于扣除超时时间）
            if staff_wait_elapsed > 0:
                message_wrapper['staff_wait_elapsed'] = staff_wait_elapsed
                self.logger.debug(
                    f"用户 {self.user_id} 人工等待耗时{staff_wait_elapsed}秒，"
                    f"已记录到message_wrapper"
                )

            # AI处理器：并行运行处理 + 新消息监听 + 人工回复监听
            ai_start_time = time.time()
            original_content = context.content if context.type == ContextType.TEXT else None
            original_kwargs = dict(context.kwargs)
            from_uid = context.kwargs.get('from_uid', '')

            ai_task = asyncio.create_task(handler.handle(context, message_wrapper))
            queue_task = asyncio.create_task(self.message_queue.get())
            # 创建人工回复监听任务，超时设置为AI处理的最大超时时间
            from Message.staff_reply_event import staff_reply_event_manager
            staff_reply_event_id = staff_reply_event_manager.start_waiting(from_uid)
            staff_reply_task = asyncio.create_task(
                staff_reply_event_manager.wait_for_staff_reply(from_uid, staff_reply_event_id, timeout=300)
            )

            try:
                while True:
                    done, pending_tasks = await asyncio.wait(
                        {ai_task, queue_task, staff_reply_task},
                        return_when=asyncio.FIRST_COMPLETED
                    )

                    if ai_task in done:
                        # AI完成了（正常或异常）
                        queue_task.cancel()
                        staff_reply_task.cancel()
                        try:
                            await queue_task
                            await staff_reply_task
                        except asyncio.CancelledError:
                            pass
                        self._ai_pending = None
                        break

                    elif queue_task in done:
                        # 新消息到了
                        staff_reply_task.cancel()
                        try:
                            await staff_reply_task
                        except asyncio.CancelledError:
                            pass
                        new_msg = queue_task.result()
                        elapsed = time.time() - ai_start_time

                        if elapsed < self.CANCEL_WINDOW:
                            # 在取消窗口内 → 取消AI，收集新消息，设置pending
                            ai_task.cancel()
                            try:
                                await ai_task
                            except (asyncio.CancelledError, Exception) as e:
                                # CancelledError 是正常的取消
                                # 其他异常是AI处理内部的错误，也需要捕获以继续取消重发流程
                                if not isinstance(e, asyncio.CancelledError):
                                    self.logger.warning(
                                        f"用户 {self.user_id} 取消AI任务时捕获到内部异常: {e}"
                                    )

                            chat_info = context.kwargs.get('_coze_chat_info')
                            self.logger.info(
                                f"用户 {self.user_id} AI超时中断({elapsed:.1f}s)，"
                                f"收到新消息，准备取消重发"
                            )

                            # 防抖收集后续新消息
                            buffered_new = [new_msg]
                            await self._collect_with_debounce(buffered_new)

                            # 设置pending，由主循环的_do_cancel_resend处理
                            self._ai_pending = {
                                'chat_info': chat_info,
                                'original_content': original_content,
                                'original_kwargs': original_kwargs,
                                'buffered_new': buffered_new,
                                'elapsed': elapsed,
                            }
                            return
                        else:
                            # 超出取消窗口 → 新消息放回队列，继续等AI
                            self.logger.debug(
                                f"用户 {self.user_id} AI已处理{elapsed:.1f}s(>{self.CANCEL_WINDOW}s窗口)，"
                                f"新消息放回队列继续等待"
                            )
                            await self.message_queue.put(new_msg)
                            # 创建新的 queue_task 继续监听
                            queue_task = asyncio.create_task(self.message_queue.get())
                            # 继续下一轮wait，AI如果完成了自然返回

                    elif staff_reply_task in done:
                        # 人工客服回复了，取消AI处理
                        staff_replied = staff_reply_task.result()
                        ai_task.cancel()
                        queue_task.cancel()
                        try:
                            await ai_task
                            await queue_task
                        except asyncio.CancelledError:
                            pass
                        
                        self.logger.info(
                            f"用户 {self.user_id} AI处理过程中收到人工客服回复，取消AI流程"
                        )
                        self._ai_pending = None
                        return
            except Exception as e:
                ai_task.cancel()
                queue_task.cancel()
                self.logger.error(f"用户 {self.user_id} 并行处理异常: {e}")
                raise
            finally:
                # 确保无论什么情况都清理人工等待状态
                staff_reply_event_manager.stop_waiting(from_uid, staff_reply_event_id)

        except Exception as e:
            self.logger.error(f"用户 {self.user_id} 处理消息 {message_id} 时发生异常: {e}")

    async def _do_cancel_resend(self):
        """执行取消重发：从_ai_pending取信息，调用handler重发"""
        pending = self._ai_pending
        self._ai_pending = None

        if not pending or not pending.get('buffered_new'):
            self.logger.warning(f"用户 {self.user_id} _do_cancel_resend但无buffered_new")
            return

        buffered_new = pending['buffered_new']
        chat_info = pending.get('chat_info')
        original_content = pending.get('original_content')
        original_kwargs = pending.get('original_kwargs')
        elapsed = pending.get('elapsed', 0)

        # 合并新消息
        if len(buffered_new) == 1:
            new_context = buffered_new[0]['context']
            new_kwargs = buffered_new[0].copy()
        else:
            # 合并多条新消息
            last_ctx = buffered_new[-1]['context']
            merged_parts = []
            for msg_w in buffered_new:
                part = self._context_to_text(msg_w['context'])
                if part:
                    merged_parts.append(part)
            merged_text = "\n".join(merged_parts)
            new_context = Context(
                type=ContextType.TEXT,
                content=merged_text,
                channel_type=last_ctx.channel_type,
                kwargs=last_ctx.kwargs
            )
            new_kwargs = buffered_new[-1].copy()
            new_kwargs['context'] = new_context

        # 检查人工客服是否已回复（新增！）
        staff_replied, _ = await self._check_staff_reply(new_context)
        if staff_replied:
            # 人工客服已回复，取消旧的 Coze chat 并直接返回
            if chat_info:
                bot = self._find_ai_bot()
                if bot and hasattr(bot, 'cancel_chat'):
                    try:
                        bot.cancel_chat(chat_info['chat_id'], chat_info['conversation_id'])
                        self.logger.info(f"用户 {self.user_id} 人工已回复，取消旧chat")
                    except Exception as e:
                        self.logger.warning(f"用户 {self.user_id} 取消旧chat失败: {e}")
            return

        # 先走非AI处理器（如关键词检测），返回False则继续找下一个处理器
        # 取消重发时，新消息也应该经过关键词检测
        handled_by_non_ai = False
        for h in self.handlers:
            if not hasattr(h, '_get_ai_reply') and h.can_handle(new_context):
                success = await h.handle(new_context, new_kwargs)
                if success:
                    # 关键词处理器已完全处理（如匹配到"转人工"或全部内容被关键词覆盖）
                    # 但还需要取消旧的 Coze chat，避免 conversation occupied
                    if chat_info:
                        bot = self._find_ai_bot()
                        if bot and hasattr(bot, 'cancel_chat'):
                            try:
                                bot.cancel_chat(chat_info['chat_id'], chat_info['conversation_id'])
                                self.logger.info(f"用户 {self.user_id} 关键词已处理，取消旧chat")
                            except Exception as e:
                                self.logger.warning(f"用户 {self.user_id} 取消旧chat失败: {e}")
                    self.logger.info(f"用户 {self.user_id} 取消重发前关键词处理器已处理")
                    return
                # 返回False说明还有剩余内容需要AI处理，继续查找
                # 将修改后的context更新到new_kwargs
                new_kwargs['context'] = new_context
                handled_by_non_ai = True
                break

        # 找AI处理器
        ai_handler = None
        for h in self.handlers:
            if h.can_handle(new_context) and hasattr(h, '_cancel_and_resend'):
                ai_handler = h
                break

        if not ai_handler or not chat_info or not original_content:
            # chat_info 为空说明 AI 还没来得及创建对话就收到新消息，这是正常的边界情况
            self.logger.info(f"用户 {self.user_id} AI尚未创建对话，新消息走正常处理流程")
            # 即使降级处理，也需要先取消旧的 Coze chat，否则会报 "Conversation occupied"
            if chat_info and ai_handler:
                try:
                    bot = getattr(ai_handler, 'bot', None)
                    if bot and hasattr(bot, 'cancel_chat'):
                        # cancel_chat 是同步方法，不需要 await
                        bot.cancel_chat(chat_info['chat_id'], chat_info['conversation_id'])
                        self.logger.info(f"用户 {self.user_id} 降级处理前已取消旧 chat")
                except Exception as e:
                    self.logger.warning(f"用户 {self.user_id} 降级处理取消旧 chat 失败: {e}")
            await self._process_single_message(new_kwargs)
            return

        # 构建重发信息传给handler
        resend_info = {
            'chat_info': chat_info,
            'original_content': original_content,
            'original_kwargs': original_kwargs,
            'cancel_used_time': elapsed,
        }
        new_kwargs['pending_ai_info'] = resend_info

        self.logger.info(
            f"用户 {self.user_id} 执行取消重发: "
            f"原query长度={len(original_content)}, 新消息数={len(buffered_new)}, "
            f"已用时间={elapsed:.1f}s"
        )

        try:
            success = await ai_handler.handle(new_context, new_kwargs)
            if not success:
                self.logger.warning(f"用户 {self.user_id} 取消重发失败")
        except Exception as e:
            self.logger.error(f"用户 {self.user_id} 取消重发异常: {e}", exc_info=True)
            success = False
    
    def _find_ai_bot(self):
        """从处理器链中找到AI Bot实例"""
        for h in self.handlers:
            bot = getattr(h, 'bot', None)
            if bot and hasattr(bot, 'cancel_chat'):
                return bot
        return None

    async def stop(self):
        """停止用户消息处理器"""
        if self.processor_task and not self.processor_task.done():
            self.processor_task.cancel()
            try:
                await self.processor_task
            except asyncio.CancelledError:
                pass
        self.is_processing = False


class MessageConsumer:
    """消息消费者 - 支持按用户分组的串行处理"""
    
    def __init__(self, queue_name: str, max_concurrent: int = 10):
        """
        初始化消息消费者
        
        Args:
            queue_name: 要消费的队列名称
            max_concurrent: 最大并发处理数（不同用户的并发数）
        """
        self.queue_name = queue_name
        self.max_concurrent = max_concurrent
        self.handlers: list[MessageHandler] = []
        self.is_running = False
        self.logger = get_logger()
        self._semaphore = None  # 惰性初始化，绑定到实际使用的 event loop
        
        # 用户消息处理器字典 {user_id: UserSequentialProcessor}
        self.user_processors: Dict[str, UserSequentialProcessor] = {}
        self.cleanup_task = None

    @property
    def semaphore(self) -> asyncio.Semaphore:
        """惰性获取 semaphore，确保绑定到当前 event loop"""
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.max_concurrent)
        return self._semaphore
        
    def add_handler(self, handler: MessageHandler):
        """
        添加消息处理器
        
        Args:
            handler: 消息处理器实例
        """
        self.handlers.append(handler)
        self.logger.debug(f"添加消息处理器: {handler.__class__.__name__}")
    
    def add_type_handler(self, message_types: set, handler_func: Callable[[Context, Dict[str, Any]], Awaitable[bool]]):
        """
        添加基于消息类型的处理器
        
        Args:
            message_types: 支持的消息类型集合
            handler_func: 处理函数
        """
        handler = TypeBasedHandler(message_types, handler_func)
        self.add_handler(handler)
    
    def add_channel_handler(self, channel_types: set, handler_func: Callable[[Context, Dict[str, Any]], Awaitable[bool]]):
        """
        添加基于渠道类型的处理器
        
        Args:
            channel_types: 支持的渠道类型集合
            handler_func: 处理函数
        """
        handler = ChannelBasedHandler(channel_types, handler_func)
        self.add_handler(handler)
    
    def _get_user_id(self, context: Context) -> str:
        """从Context中提取用户ID"""
        from_uid = context.kwargs.get('from_uid')
        channel = context.channel_type
        user_id = channel.value + "_" + from_uid
        return str(user_id)
    
    def _get_or_create_user_processor(self, user_id: str) -> UserSequentialProcessor:
        """获取或创建用户消息处理器"""
        if user_id not in self.user_processors:
            processor = UserSequentialProcessor(user_id, self.handlers)
            self.user_processors[user_id] = processor
            self.logger.debug(f"为用户 {user_id} 创建消息处理器")
        
        return self.user_processors[user_id]
    
    async def _process_message(self, message_wrapper: Dict[str, Any]):
        """
        处理单条消息 - 分配给对应用户的处理器
        
        Args:
            message_wrapper: 消息包装器
        """
        async with self.semaphore:
            context = message_wrapper['context']
            user_id = self._get_user_id(context)
            
            # 获取或创建用户处理器
            user_processor = self._get_or_create_user_processor(user_id)
            
            # 将消息添加到用户处理器的队列中
            await user_processor.add_message(message_wrapper)
            
            self.logger.debug(f"消息已分配给用户 {user_id} 的处理器")

    async def start(self):
        """启动消息消费者"""
        if self.is_running:
            self.logger.warning(f"消费者 {self.queue_name} 已在运行")
            return
            
        self.is_running = True
        self.logger.debug(f"启动消息消费者: {self.queue_name}")
        
        # 获取或创建队列
        queue = message_queue_manager.get_or_create_queue(self.queue_name)
        
        # 启动清理任务
        self.cleanup_task = asyncio.create_task(self._cleanup_inactive_processors())
        
        try:
            while self.is_running:
                # 从队列获取消息
                message_wrapper = await queue.get(timeout=1.0)
                
                if message_wrapper is None:
                    continue
                
                # 异步处理消息（分配给用户处理器）
                asyncio.create_task(self._process_message(message_wrapper))
                
        except Exception as e:
            self.logger.error(f"消费者 {self.queue_name} 运行时发生错误: {e}")
        finally:
            self.is_running = False
            
            # 停止清理任务
            if self.cleanup_task:
                self.cleanup_task.cancel()
                try:
                    await self.cleanup_task
                except asyncio.CancelledError:
                    pass
            
            # 停止所有用户处理器
            await self._stop_all_user_processors()
            
            self.logger.debug(f"消息消费者 {self.queue_name} 已停止")
    
    async def _cleanup_inactive_processors(self):
        """定期清理不活跃的用户处理器"""
        try:
            while self.is_running:
                await asyncio.sleep(60)  # 每分钟检查一次
                
                inactive_users = []
                for user_id, processor in self.user_processors.items():
                    if not processor.is_processing:
                        inactive_users.append(user_id)
                
                # 清理不活跃的处理器
                for user_id in inactive_users:
                    processor = self.user_processors.pop(user_id, None)
                    if processor:
                        if hasattr(processor, 'stop') and callable(getattr(processor, 'stop')):
                            await processor.stop()
                        self.logger.debug(f"清理用户 {user_id} 的不活跃处理器")
                        
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.logger.error(f"清理任务异常: {e}")
    
    async def _stop_all_user_processors(self):
        """停止所有用户处理器"""
        tasks = []
        for processor in self.user_processors.values():
            if hasattr(processor, 'stop') and callable(getattr(processor, 'stop')):
                tasks.append(processor.stop())
        
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        
        self.user_processors.clear()
        self.logger.debug("所有用户处理器已停止")

    async def stop(self):
        """停止消息消费者"""
        self.is_running = False
        self.logger.debug(f"正在停止消息消费者: {self.queue_name}")


class MessageConsumerManager:
    """消息消费者管理器"""
    
    def __init__(self):
        self.consumers: Dict[str, MessageConsumer] = {}
        self.consumer_tasks: Dict[str, asyncio.Task] = {}
        self.logger = get_logger()
    
    def create_consumer(self, queue_name: str, max_concurrent: int = 10) -> MessageConsumer:
        """
        创建消息消费者
        
        Args:
            queue_name: 队列名称
            max_concurrent: 最大并发处理数
            
        Returns:
            MessageConsumer实例
        """
        if queue_name in self.consumers:
            raise ValueError(f"消费者 '{queue_name}' 已存在")
            
        consumer = MessageConsumer(queue_name, max_concurrent)
        self.consumers[queue_name] = consumer
        self.logger.debug(f"创建消息消费者: {queue_name}")
        
        return consumer
    
    def get_consumer(self, queue_name: str) -> Optional[MessageConsumer]:
        """
        获取消息消费者
        
        Args:
            queue_name: 队列名称
            
        Returns:
            MessageConsumer实例或None
        """
        return self.consumers.get(queue_name)
    
    async def start_consumer(self, queue_name: str):
        """
        启动消息消费者
        
        Args:
            queue_name: 队列名称
        """
        consumer = self.consumers.get(queue_name)
        if not consumer:
            raise ValueError(f"消费者 '{queue_name}' 不存在")
            
        if queue_name in self.consumer_tasks:
            self.logger.warning(f"消费者 {queue_name} 已在运行")
            return
            
        # 创建消费者任务
        task = asyncio.create_task(consumer.start())
        self.consumer_tasks[queue_name] = task
        self.logger.debug(f"启动消费者任务: {queue_name}")
    
    async def stop_consumer(self, queue_name: str):
        """
        停止消息消费者
        
        Args:
            queue_name: 队列名称
        """
        consumer = self.consumers.get(queue_name)
        if consumer:
            await consumer.stop()
            # 从字典中移除，否则下次启动时 get_consumer 会找到旧的、已停止的 consumer 而跳过创建
            del self.consumers[queue_name]
            
        # 取消并清理任务
        task = self.consumer_tasks.get(queue_name)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            del self.consumer_tasks[queue_name]
            self.logger.debug(f"停止消费者任务: {queue_name}")
    
    async def stop_all_consumers(self):
        """停止所有消费者"""
        tasks = []
        for queue_name in list(self.consumers.keys()):
            tasks.append(self.stop_consumer(queue_name))
        
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        
        self.logger.debug("所有消费者已停止")
    
    def list_consumers(self) -> List[str]:
        """获取所有消费者名称"""
        return list(self.consumers.keys())
    
    def get_running_consumers(self) -> List[str]:
        """获取正在运行的消费者名称"""
        return list(self.consumer_tasks.keys())


# 全局消息消费者管理器实例
message_consumer_manager = MessageConsumerManager() 