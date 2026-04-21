"""
增强版AI回复处理器 - 集成限流、兜底回复、关键词检测等功能

基于上游新架构重实现
"""

import random
from typing import Dict, Any, Optional, Set, List
from bridge.context import Context, ContextType
from .base import BaseHandler
from .preprocessor import MessagePreprocessor
from .keyword_matcher import matcher_factory
from .rate_limiter import coze_rate_limiter
from Agent.bot import Bot
from utils.logger_loguru import get_logger

logger = get_logger(__name__)


class EnhancedAIReplyHandler(BaseHandler):
    """增强版AI回复处理器 - 集成所有高级功能"""

    # AI回复兜底模式关键词（当AI回复包含这些关键词时，替换为配置的兜底回复）
    FALLBACK_PATTERNS = [
        "不知道",
        "不清楚",
        "不了解",
        "帮你反馈",
        "反馈给",
        "技术人员",
        "无法回答",
        "无法解答"
    ]

    # 默认兜底回复（限流和AI无回复时使用）
    DEFAULT_FALLBACK_REPLIES = [
        "这个我不了解呢，帮你问下我们的技术人员",
        "抱歉，这个问题我需要查询一下",
        "请稍等，我让技术人员来回复您"
    ]

    def __init__(self, bot: Bot = None, auto_reply_types: Set[ContextType] = None,
                 enable_fallback: bool = True, fallback_replies: List[str] = None):
        """
        初始化增强版AI回复处理器

        Args:
            bot: AI Bot实例
            auto_reply_types: 支持自动回复的消息类型
            enable_fallback: 是否启用兜底回复
            fallback_replies: 自定义兜底回复列表
        """
        super().__init__("EnhancedAIReplyHandler")
        self.bot = bot
        self.preprocessor = MessagePreprocessor()
        self.enable_fallback = enable_fallback
        self.fallback_replies = fallback_replies or self.DEFAULT_FALLBACK_REPLIES
        self.auto_reply_types = auto_reply_types or {
            ContextType.TEXT,
            ContextType.GOODS_INQUIRY,
            ContextType.GOODS_SPEC,
            ContextType.ORDER_INFO,
            ContextType.IMAGE,
            ContextType.VIDEO,
            ContextType.EMOTION
        }

        # 从 DI 容器获取 CustomerAgent（如果未传入）
        if bot is None:
            try:
                from core.di_container import container
                from Agent.CustomerAgent.agent import CustomerAgent
                self.bot = container.get(CustomerAgent)
            except Exception as e:
                logger.warning(f"从DI容器获取CustomerAgent失败: {e}, 将使用无Bot模式")

        # 初始化关键词匹配器
        self.matcher_factory = matcher_factory

        # 加载限流配置
        self._load_rate_limit_config()

    def _load_rate_limit_config(self):
        """从 config 加载限流配置"""
        try:
            from config import config
            rc = config.get_rate_limit_config()
            coze_rate_limiter.configure(
                window_size=rc['window_hours'] * 3600,
                max_requests=rc['max_requests']
            )
            # 加载兜底回复
            if rc.get('fallback_reply'):
                self.fallback_replies = rc['fallback_reply']
            logger.info(f"限流配置已加载: {rc}")
        except Exception as e:
            logger.warning(f"加载限流配置失败: {e}，使用默认配置")

    def can_handle(self, context: Context) -> bool:
        """检查是否可以处理该消息"""
        return context.type in self.auto_reply_types

    async def handle(self, context: Context, metadata: Dict[str, Any]) -> bool:
        """处理AI回复"""
        try:
            # 1. 检查限流
            from_uid = metadata.get('from_uid')
            if from_uid and coze_rate_limiter.is_rate_limited(from_uid):
                self.logger.warning(f"用户 {from_uid} 已被限流，使用兜底回复")
                return await self._send_fallback_reply(context, metadata)

            # 2. 预处理消息
            processed_content = self.preprocessor.process(context.content, context.type)

            # 3. 调用AI生成回复
            reply = await self._get_ai_reply(processed_content, context)

            # 4. 检查AI回复是否需要替换为兜底
            if reply and self._needs_fallback(reply):
                self.logger.info(f"AI回复包含兜底关键词，替换为兜底回复")
                reply = self._get_random_fallback()

            # 5. 如果没有AI回复，使用兜底
            if not reply:
                self.logger.warning("AI回复生成失败，使用兜底回复")
                reply = self._get_random_fallback()

            # 6. 发送回复
            success = await self._send_reply(context, reply, metadata)
            if success:
                await self.log_message(context, "AI回复发送成功", f"回复: {reply[:50]}...")
            else:
                self.logger.warning("AI回复发送失败")
                return await self._send_fallback_reply(context, metadata)

            return True

        except Exception as e:
            self.logger.error(f"AI回复处理失败: {e}")
            return await self._send_fallback_reply(context, metadata)

    def _needs_fallback(self, reply: str) -> bool:
        """检查AI回复是否需要替换为兜底"""
        if not self.enable_fallback:
            return False

        reply_lower = reply.lower()
        for pattern in self.FALLBACK_PATTERNS:
            if pattern.lower() in reply_lower:
                return True
        return False

    def _get_random_fallback(self) -> str:
        """获取随机兜底回复"""
        if not self.fallback_replies:
            return self.DEFAULT_FALLBACK_REPLIES[0]
        return random.choice(self.fallback_replies)

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
                logger.warning("Bot不支持reply或async_reply方法")
                return None

        except Exception as e:
            logger.error(f"AI Bot调用失败: {e}")
            return None

    async def _send_reply(self, context: Context, reply: str, metadata: Dict[str, Any]) -> bool:
        """发送回复"""
        try:
            # 从metadata中提取必要信息
            shop_id = metadata.get('shop_id')
            user_id = metadata.get('user_id')
            from_uid = metadata.get('from_uid')

            if not all([shop_id, user_id, from_uid]):
                logger.warning(f"缺少发送信息: shop_id={shop_id}, user_id={user_id}, from_uid={from_uid}")
                return False

            # 尝试发送消息
            from Channel.pinduoduo.utils.API.send_message import SendMessage
            sender = SendMessage(shop_id, user_id)
            result = sender.send_text(from_uid, reply)
            if isinstance(result, dict) and result.get("success"):
                return True
            return False

        except Exception as e:
            logger.error(f"发送回复失败: {e}")
            return False

    async def _send_fallback_reply(self, context: Context, metadata: Dict[str, Any]) -> bool:
        """发送兜底回复"""
        try:
            fallback_reply = self._get_random_fallback()
            self.logger.info(f"使用兜底回复: {fallback_reply}")

            # 尝试发送兜底回复
            success = await self._send_reply(context, fallback_reply, metadata)
            if not success:
                # 如果发送失败，至少记录日志
                await self.log_message(context, "兜底回复发送失败", f"内容: {fallback_reply}")
                # 但仍然返回True，表示消息已处理
                return True

            await self.log_message(context, "兜底回复发送成功", f"内容: {fallback_reply}")
            return True

        except Exception as e:
            logger.error(f"兜底回复处理失败: {e}")
            return True  # 即使失败也返回True，避免重复处理
