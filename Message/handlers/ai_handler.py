"""
AI回复处理器
专注的AI处理，移除复杂预处理和发送逻辑
"""

from typing import Dict, Any, Optional, Tuple
import asyncio
from bridge.context import Context, ContextType
from .base import BaseHandler
from .preprocessor import MessagePreprocessor
from .keyword_matcher import find_matches
from Agent.bot import Bot
from utils.logger_loguru import get_logger

logger = get_logger("AIReplyHandler")


class AIReplyHandler(BaseHandler):
    """专注的AI回复处理器"""

    def __init__(self, bot: Optional[Bot] = None, auto_reply_types: Optional[set] = None):
        super().__init__("AIReplyHandler")
        # 从 DI 容器获取 CustomerAgent（如果未传入）
        if bot is None:
            try:
                from core.di_container import container
                from Agent.CustomerAgent.agent import CustomerAgent
                bot = container.get(CustomerAgent)
            except Exception as e:
                logger.warning(f"从DI容器获取CustomerAgent失败: {e}, 将使用无Bot模式")
        self.bot = bot
        self.preprocessor = MessagePreprocessor()
        self.auto_reply_types = auto_reply_types or {
            ContextType.TEXT,
            ContextType.GOODS_INQUIRY,
            ContextType.GOODS_SPEC,
            ContextType.ORDER_INFO,
            ContextType.IMAGE,
            ContextType.VIDEO,
            ContextType.EMOTION
        }

        # 加载限流配置（消费者层 is_rate_limited 检查依赖此配置）
        self._load_rate_limit_config()

    def _load_rate_limit_config(self):
        """从 config 加载限流配置到全局限流器"""
        try:
            from config import config
            rc = config.get_rate_limit_config()
            from Message.handlers.rate_limiter import coze_rate_limiter
            coze_rate_limiter.configure(
                window_size=rc['window_hours'] * 3600,
                max_requests=rc['max_requests']
            )
            logger.info(f"限流配置已加载: {rc}")
        except Exception as e:
            logger.warning(f"加载限流配置失败: {e}，使用默认配置")

    def can_handle(self, context: Context) -> bool:
        """检查是否可以处理该消息"""
        # 支持多种消息类型
        return context.type in self.auto_reply_types

    async def handle(self, context: Context, metadata: Dict[str, Any]) -> bool:
        """处理AI回复"""
        try:
            # 1. 预处理消息
            content = context.content if context.content else ""
            processed_content = self.preprocessor.process(content, context.type)

            # 2. 调用AI生成回复
            reply = await self._get_ai_reply(processed_content, context)
            if not reply:
                self.logger.warning("AI回复生成失败，使用备用回复")
                return await self._handle_fallback(context, metadata)

            # 2.5 发送前禁用词检查：命中则把具体禁用词反馈给 AI 重生成，最多重试 2 次；
            #      仍命中则走兜底回复，绝不把含禁用词的回复发出去。
            reply, hit_fallback = await self._check_banned_words(
                processed_content, reply, context, metadata
            )
            if hit_fallback:
                return await self._handle_fallback(context, metadata)
            if not reply:
                # 重生成链路上 AI 全部失败，走兜底（理论上 _check_banned_words 已处理）
                return await self._handle_fallback(context, metadata)

            # 3. 发送前检查：任务是否已被取消（人工客服已介入）
            from_uid = metadata.get('from_uid')
            current_task = asyncio.current_task()
            if current_task and current_task.cancelled():
                self.logger.info(f"AI回复生成完毕但任务已取消（人工已介入），跳过发送: {from_uid}")
                return True  # 返回 True 避免兜底重发

            # 二次检查：人工是否在 AI 生成期间回复（冷却期内说明人工刚回复过）
            if from_uid:
                try:
                    from Message.handlers.staff_reply_event import staff_reply_event_manager
                    if staff_reply_event_manager.is_in_cooldown(str(from_uid)):
                        self.logger.info(f"AI回复生成完毕但人工已回复，丢弃AI回复: {from_uid}")
                        return True
                except Exception as e:
                    logger.warning(f"检查人工冷却期失败: {e}")

            # 4. 发送回复
            success = await self._send_reply(context, reply, metadata, reply_source='ai')
            if success:
                await self.log_message(context, "AI回复发送成功", f"回复: {reply}...")
            else:
                self.logger.warning("AI回复发送失败")
                return await self._handle_fallback(context, metadata)

            return True

        except Exception as e:
            self.logger.error(f"AI回复处理失败: {e}")
            return await self._handle_fallback(context, metadata)

    async def _get_ai_reply(self, query: str, context: Context) -> Optional[str]:
        """获取AI回复"""
        if not self.bot:
            return None

        try:
            # 优先使用异步接口，其次回退到同步接口
            if hasattr(self.bot, 'async_reply'):
                res = await self.bot.async_reply(query, context)
                return getattr(res, 'content', str(res))
            elif hasattr(self.bot, 'reply'):
                res = self.bot.reply(query, context)
                return getattr(res, 'content', str(res))
            else:
                self.logger.warning("Bot不支持reply或async_reply方法")
                return None

        except Exception as e:
            self.logger.error(f"AI Bot调用失败: {e}")
            return None

    async def _send_reply(self, context: Context, reply: str, metadata: Dict[str, Any],
                          reply_source: str = 'ai') -> bool:
        """发送回复并持久化

        Args:
            reply_source: 回复来源标记，'ai' 或 'fallback'
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
            # 使用 asyncio.to_thread 包装同步 requests 调用，避免阻塞事件循环
            # （事件循环需保持响应，以便 staff_reply_task 的 event.set() 通知能及时投递）
            from Channel.pinduoduo.utils.API.send_message import SendMessage
            sender = SendMessage(str(shop_id), str(user_id))
            result = await asyncio.to_thread(sender.send_text, str(from_uid), reply)
            if isinstance(result, dict) and result.get("success"):
                # === 持久化回复消息（接入点B） ===
                try:
                    from services.message_persistence import message_persistence_service
                    msg_dict = message_persistence_service.save_outbound_message(
                        shop_id=str(shop_id), user_id=str(user_id),
                        buyer_uid=str(from_uid), reply_content=reply, reply_source=reply_source
                    )
                    if msg_dict:
                        message_persistence_service.notify_new_message(msg_dict)
                except Exception as e:
                    logger.warning(f"持久化回复失败: {e}")
                return True
            return False

        except Exception as e:
            self.logger.error(f"发送回复失败: {e}")
            return False

    async def _handle_fallback(self, context: Context, metadata: Dict[str, Any]) -> bool:
        """备用回复处理"""
        try:
            # 简单的自动回复
            reply_text = "亲，感谢您的咨询！客服正在为您处理，请稍等片刻。"

            # 记录备用回复
            self.logger.info("使用备用回复")

            # 尝试发送备用回复（reply_source='fallback'）
            success = await self._send_reply(context, reply_text, metadata, reply_source='fallback')
            if not success:
                # 如果发送失败，至少记录日志
                await self.log_message(context, "备用回复发送失败", f"内容: {reply_text}")
                # 但仍然返回True，表示消息已处理
                return True

            await self.log_message(context, "备用回复发送成功", f"内容: {reply_text}")
            return True

        except Exception as e:
            self.logger.error(f"备用回复处理失败: {e}")
            return True  # 即使失败也返回True，避免重复处理

    async def _check_banned_words(
        self, query: str, reply: str, context: Context, metadata: Dict[str, Any]
    ) -> Tuple[Optional[str], bool]:
        """发送前禁用词检查。

        命中禁用词时，把具体命中的词拼进约束提示，让 AI 基于上一版（agno session
        历史已含上一版回复）重新生成合规回复，最多重试 ``max_retry`` 次。
        重试耗尽仍命中，返回 ``(None, True)`` 表示应走兜底回复。
        """
        from config import config as _cfg

        banned = _cfg.get("banned_words", []) or []
        if not banned:
            return reply, False

        max_retry = 2
        current = reply
        for _ in range(max_retry + 1):
            hits = find_matches(banned, current)
            if not hits:
                return current, False
            constraint = (
                f"{query}" + chr(10) + f"[系统约束] 你上一条回复包含以下禁用词：{hits}。"
                f"这些词严禁发送给客户。请重新生成一份不含这些词的合规回复，"
                f"保持原意、话术风格与简洁度。"
            )
            regen = await self._get_ai_reply(constraint, context)
            if not regen:
                break
            current = regen

        if find_matches(banned, current):
            self.logger.warning(f"禁用词重试{max_retry}次仍命中，走兜底回复: {find_matches(banned, current)}")
            return None, True
        return current, False
