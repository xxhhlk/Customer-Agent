"""
用户消息顺序处理器 - 支持防抖合并 + AI超时中断重发 + 夜间延迟 + 频率限制

基于上游新架构重实现
"""

import time
import asyncio
import random
from datetime import datetime, time as dt_time
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass
from bridge.context import Context
from utils.logger_loguru import get_logger
from utils.rate_limiter import UserRateLimiterManager
from config import config

logger = get_logger(__name__)


@dataclass
class PendingMessage:
    """待处理消息封装"""
    context: Context
    metadata: Dict[str, Any]
    timestamp: float


class UserSequentialProcessor:
    """用户消息顺序处理器 - 支持防抖合并 + AI超时中断重发 + 夜间延迟 + 频率限制"""

    # 防抖等待时间（秒）
    DEBOUNCE_SECONDS = 8
    # 夜间防抖等待时间（秒）- 5分钟
    NIGHT_DEBOUNCE_SECONDS = 300
    # 夜间时段配置（小时:分钟）
    NIGHT_START = dt_time(23, 1)  # 23:01
    NIGHT_END = dt_time(7, 55)    # 07:55

    # AI超时中断重发相关配置
    REPLY_TIMEOUT = 165.0          # AI回复总超时（秒）
    CANCEL_WINDOW = 25.0           # 超时中断等待窗口（秒）
    MIN_REPLY_TIMEOUT = 120.0      # 取消重发后的最低超时（秒）

    # 类级别的频率限制器管理器（所有用户共享）
    _rate_limiter_manager: Optional[UserRateLimiterManager] = None

    @classmethod
    def _get_rate_limiter_manager(cls) -> UserRateLimiterManager:
        """获取频率限制器管理器（懒加载）"""
        if cls._rate_limiter_manager is None:
            rate_limit_config = config.get_rate_limit_config()
            window_seconds = int(rate_limit_config['window_hours'] * 3600)
            max_requests = rate_limit_config['max_requests']
            cls._rate_limiter_manager = UserRateLimiterManager(
                window_seconds=window_seconds,
                max_requests=max_requests
            )
            logger.info(
                f"初始化频率限制器: 窗口={rate_limit_config['window_hours']}小时, "
                f"最大请求={max_requests}次"
            )
        return cls._rate_limiter_manager

    def __init__(self, user_id: str, process_func):
        """
        初始化用户消息处理器

        Args:
            user_id: 用户标识
            process_func: 实际的消息处理函数 async func(context, metadata) -> bool
        """
        self.user_id = user_id
        self.process_func = process_func
        self.pending_messages: List[PendingMessage] = []
        self.is_processing = False
        self.debounce_task: Optional[asyncio.Task] = None
        self.logger = get_logger(f"UserProcessor.{user_id}")

    def add_message(self, context: Context, metadata: Dict[str, Any]) -> None:
        """
        添加消息到待处理队列

        Args:
            context: 消息上下文
            metadata: 消息元数据
        """
        self.pending_messages.append(PendingMessage(
            context=context,
            metadata=metadata,
            timestamp=time.time()
        ))
        self.logger.debug(f"添加消息到队列，当前待处理: {len(self.pending_messages)}")

        # 如果不在处理中，启动防抖等待
        if not self.is_processing:
            self._start_debounce_wait()

    def _start_debounce_wait(self) -> None:
        """启动防抖等待任务"""
        if self.debounce_task and not self.debounce_task.done():
            self.debounce_task.cancel()

        self.debounce_task = asyncio.create_task(self._debounce_wait_loop())

    async def _debounce_wait_loop(self) -> None:
        """防抖等待循环"""
        wait_seconds = self._get_current_debounce_seconds()
        self.logger.debug(f"开始防抖等待: {wait_seconds}秒")

        try:
            await asyncio.sleep(wait_seconds)
            # 等待结束，开始处理
            await self._process_batch()
        except asyncio.CancelledError:
            self.logger.debug("防抖等待被取消（有新消息）")

    def _get_current_debounce_seconds(self) -> float:
        """获取当前应该使用的防抖等待时间"""
        now = datetime.now().time()

        # 检查是否在夜间时段
        if self.NIGHT_START <= now or now < self.NIGHT_END:
            self.logger.debug(f"当前时段 {now} 为夜间时段，使用夜间防抖时间: {self.NIGHT_DEBOUNCE_SECONDS}秒")
            return self.NIGHT_DEBOUNCE_SECONDS

        return self.DEBOUNCE_SECONDS

    async def _process_batch(self) -> None:
        """批量处理待处理消息"""
        if not self.pending_messages:
            return

        self.is_processing = True
        messages_to_process = self.pending_messages.copy()
        self.pending_messages = []

        try:
            self.logger.info(f"开始批量处理 {len(messages_to_process)} 条消息")

            # 合并消息内容
            merged_context, merged_metadata = self._merge_messages(messages_to_process)

            # 调用处理函数（带AI超时中断重发）
            success = await self._process_with_timeout_retry(merged_context, merged_metadata)

            if success:
                self.logger.info(f"批量消息处理成功")
            else:
                self.logger.warning(f"批量消息处理失败")

        except Exception as e:
            self.logger.error(f"批量消息处理异常: {e}")
        finally:
            self.is_processing = False

            # 检查是否有新消息到达
            if self.pending_messages:
                self.logger.debug("处理期间有新消息到达，继续防抖等待")
                self._start_debounce_wait()

    def _merge_messages(self, messages: List[PendingMessage]) -> Tuple[Context, Dict[str, Any]]:
        """
        合并多条消息为一条

        Args:
            messages: 待合并的消息列表

        Returns:
            (merged_context, merged_metadata)
        """
        if len(messages) == 1:
            return messages[0].context, messages[0].metadata

        # 使用第一条消息作为基础
        first_msg = messages[0]
        merged_context = first_msg.context
        merged_metadata = first_msg.metadata.copy()

        # 合并内容
        merged_contents = []
        for msg in messages:
            if hasattr(msg.context, 'content') and msg.context.content:
                merged_contents.append(str(msg.context.content))

        if merged_contents:
            merged_content = "\n".join(merged_contents)
            if hasattr(merged_context, 'content'):
                merged_context.content = merged_content

        merged_metadata['merged_count'] = len(messages)
        self.logger.debug(f"合并了 {len(messages)} 条消息")

        return merged_context, merged_metadata

    async def _process_with_timeout_retry(self, context: Context, metadata: Dict[str, Any]) -> bool:
        """
        带超时中断重发的消息处理

        Args:
            context: 消息上下文
            metadata: 消息元数据

        Returns:
            bool: 是否处理成功
        """
        timeout = self.REPLY_TIMEOUT
        retry_count = 0
        max_retries = 1

        while retry_count <= max_retries:
            try:
                result = await asyncio.wait_for(
                    self._process_with_cancel_window(context, metadata, timeout),
                    timeout=timeout + 5.0  # 额外5秒缓冲
                )
                return result
            except asyncio.TimeoutError:
                retry_count += 1
                if retry_count > max_retries:
                    self.logger.error(f"AI回复多次超时，放弃处理")
                    return False

                # 计算新的超时时间（取当前超时的一半，但不低于最小值）
                timeout = max(timeout / 2, self.MIN_REPLY_TIMEOUT)
                self.logger.warning(
                    f"AI回复超时，第{retry_count}次重试，新超时: {timeout:.0f}秒"
                )

        return False

    async def _process_with_cancel_window(self, context: Context, metadata: Dict[str, Any], timeout: float) -> bool:
        """
        带取消窗口的处理：先等待CANCEL_WINDOW秒，期间可被取消

        Args:
            context: 消息上下文
            metadata: 消息元数据
            timeout: 总超时时间

        Returns:
            bool: 是否处理成功
        """
        from Message.handlers.staff_reply_event import staff_reply_event_manager

        # 先等待CANCEL_WINDOW秒，这期间如果人工回复了就取消处理
        try:
            # 启动人工回复监听
            from_uid = metadata.get('from_uid')
            event_id = None
            if from_uid:
                event_id = staff_reply_event_manager.start_waiting(from_uid)

            try:
                # 等待CANCEL_WINDOW秒，或者人工回复
                if from_uid and event_id:
                    staff_replied = await staff_reply_event_manager.wait_for_staff_reply(
                        from_uid, event_id, self.CANCEL_WINDOW
                    )
                    if staff_replied:
                        self.logger.info("检测到人工客服回复，取消AI处理")
                        return True
                else:
                    await asyncio.sleep(self.CANCEL_WINDOW)

            finally:
                # 清理事件监听
                if from_uid and event_id:
                    staff_reply_event_manager.stop_waiting(from_uid, event_id)

        except Exception as e:
            self.logger.warning(f"取消窗口等待异常: {e}，继续AI处理")

        # ========== 频率限制检查 ==========
        try:
            rate_limiter = self._get_rate_limiter_manager()
            is_allowed = await rate_limiter.is_allowed(self.user_id)

            if not is_allowed:
                # 超过频率限制，发送兜底回复
                self.logger.warning(f"用户 {self.user_id} 超过频率限制，发送兜底回复")
                return await self._send_fallback_reply(context, metadata)

            # 记录本次请求
            await rate_limiter.record_request(self.user_id)
            status = await rate_limiter.get_user_status(self.user_id)
            self.logger.debug(
                f"频率检查通过，剩余次数: {status['remaining']}/{status['request_count']}"
            )

        except Exception as e:
            self.logger.error(f"频率限制检查异常: {e}，继续AI处理")
        # ========== 频率限制检查结束 ==========

        # 取消窗口结束，开始AI处理
        remaining_timeout = max(timeout - self.CANCEL_WINDOW, 30.0)

        try:
            result = await asyncio.wait_for(
                self.process_func(context, metadata),
                timeout=remaining_timeout
            )
            return result
        except asyncio.TimeoutError:
            self.logger.error(f"AI回复超时（剩余时间: {remaining_timeout:.0f}秒）")
            raise

    async def _send_fallback_reply(self, context: Context, metadata: Dict[str, Any]) -> bool:
        """发送兜底回复（频率限制触发时）

        Args:
            context: 消息上下文
            metadata: 消息元数据

        Returns:
            bool: 是否发送成功
        """
        try:
            # 从配置获取兜底回复列表
            rate_limit_config = config.get_rate_limit_config()
            fallback_replies = rate_limit_config.get('fallback_reply', [])

            # 如果没有配置兜底回复，使用默认回复
            if not fallback_replies:
                fallback_replies = [
                    "亲，感谢您的咨询！客服正在为您处理，请稍等片刻。",
                    "您好，客服稍后会为您解答，请耐心等待~",
                    "收到您的消息啦，客服马上就来~"
                ]

            # 随机选择一条兜底回复
            reply_text = random.choice(fallback_replies)
            self.logger.info(f"发送兜底回复: {reply_text}")

            # 发送回复
            return await self._send_reply(context, reply_text, metadata)

        except Exception as e:
            self.logger.error(f"发送兜底回复失败: {e}")
            return True  # 返回True避免重复处理

    async def _send_reply(self, context: Context, reply: str, metadata: Dict[str, Any]) -> bool:
        """发送回复消息

        Args:
            context: 消息上下文
            reply: 回复内容
            metadata: 消息元数据

        Returns:
            bool: 是否发送成功
        """
        try:
            # 从metadata中提取必要信息
            shop_id = metadata.get('shop_id')
            user_id = metadata.get('user_id')
            from_uid = metadata.get('from_uid')

            if not shop_id or not user_id or not from_uid:
                self.logger.warning(f"缺少发送信息: shop_id={shop_id}, user_id={user_id}, from_uid={from_uid}")
                return False

            # 尝试发送消息
            from Channel.pinduoduo.utils.API.send_message import SendMessage
            sender = SendMessage(str(shop_id), str(user_id))
            result = sender.send_text(str(from_uid), reply)
            if isinstance(result, dict) and result.get("success"):
                return True
            return False

        except Exception as e:
            self.logger.error(f"发送回复失败: {e}")
            return False

    async def stop(self) -> None:
        """停止处理器"""
        if self.debounce_task and not self.debounce_task.done():
            self.debounce_task.cancel()
            try:
                await self.debounce_task
            except asyncio.CancelledError:
                pass
        self.logger.debug("用户处理器已停止")
