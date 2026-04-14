"""
消息处理器集合
提供各种常用的消息处理器实现
"""
from typing import Dict, Any, List, Set, Callable, Awaitable
from datetime import datetime
import asyncio
import re
import random
from concurrent.futures import ThreadPoolExecutor

from Message.message_consumer import MessageHandler
from bridge.context import Context, ContextType, ChannelType
from utils.logger import get_logger
from utils.resource_manager import ThreadResourceManager
from utils.performance_monitor import monitor_async_function
from Channel.pinduoduo.utils.API.send_message import SendMessage


class AIAutoReplyHandler(MessageHandler):
    """AI自动回复处理器 - 集成CozeBot智能回复"""
    
    # 超时中断重发相关配置
    REPLY_TIMEOUT = 165.0          # AI回复总超时（秒）
    CANCEL_WINDOW = 25.0           # 超时中断等待窗口（秒）
    MIN_REPLY_TIMEOUT = 120.0      # 取消重发后的最低超时（秒）

    # 默认兜底回复（限流和AI无回复时使用）
    DEFAULT_FALLBACK_REPLY = ["这个我不了解呢，帮你问下我们的技术人员"]
    
    def __init__(self, bot=None, auto_reply_types: Set[ContextType] = None, enable_fallback: bool = True, max_workers: int = 5):
        """
        初始化AI自动回复处理器

        Args:
            bot: AI Bot实例 (如CozeBot)
            auto_reply_types: 支持自动回复的消息类型
            enable_fallback: 是否启用规则回复作为后备
            max_workers: 线程池最大工作线程数
        """
        self.bot = bot
        self.auto_reply_types = auto_reply_types or {
            ContextType.TEXT,
            ContextType.GOODS_INQUIRY,
            ContextType.GOODS_SPEC,
            ContextType.ORDER_INFO,
            ContextType.IMAGE,
            ContextType.VIDEO,
            ContextType.EMOTION
        }
        self.enable_fallback = enable_fallback
        self.max_workers = max_workers
        self.logger = get_logger()

        # 创建专用的线程池资源管理器
        self.resource_manager = ThreadResourceManager()
        self.executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="ai_handler"
        )
        self.resource_manager.register_thread_pool(
            self.executor,
            f"AI处理器线程池(max_workers={max_workers})"
        )

        # 初始化限流器
        from Message.rate_limiter import coze_rate_limiter
        self.rate_limiter = coze_rate_limiter

        # 从 config 加载限流配置
        try:
            from config import config
            rc = config.get_rate_limit_config()
            self.rate_limiter.configure(
                window_size=rc['window_hours'] * 3600,
                max_requests=rc['max_requests']
            )
            self._fallback_reply = rc['fallback_reply']
        except Exception:
            self._fallback_reply = self.DEFAULT_FALLBACK_REPLY

        # 如果没有提供bot实例，尝试创建默认的CozeBot
        if not self.bot:
            try:
                from Agent.bot_factory import create_bot
                self.bot = create_bot()
                self.logger.debug("已创建默认AI Bot实例")
            except Exception as e:
                self.logger.warning(f"创建AI Bot失败: {e}，将使用规则回复")
                self.bot = None

    def _get_random_fallback(self) -> str:
        """
        随机获取一个兜底回复
        
        Returns:
            str: 随机选择的兜底回复
        """
        if isinstance(self._fallback_reply, list) and self._fallback_reply:
            return random.choice(self._fallback_reply)
        elif isinstance(self._fallback_reply, str):
            return self._fallback_reply
        else:
            return self.DEFAULT_FALLBACK_REPLY[0]

    def __del__(self):
        """析构函数，确保线程池被正确关闭"""
        try:
            if hasattr(self, 'resource_manager') and hasattr(self, 'executor'):
                # 尝试在现有 event loop 中清理，如果没有则直接关闭 executor
                try:
                    loop = asyncio.get_running_loop()
                    # 有运行中的 loop，创建清理任务（但不等待，因为 __del__ 不能 await）
                    loop.create_task(self.resource_manager.cleanup_all())
                except RuntimeError:
                    # 没有运行中的 event loop，同步关闭 executor
                    pass
                finally:
                    # 无论哪种情况，都关闭 executor
                    self.executor.shutdown(wait=False)
        except Exception as e:
            self.logger.error(f"清理AI处理器资源失败: {e}")
    
    def can_handle(self, context: Context) -> bool:
        """检查是否可以处理该消息"""
        # 支持拼多多渠道的多种消息类型
        return (context.type in self.auto_reply_types and 
                context.channel_type == ChannelType.PINDUODUO)
    
    def _preprocess_message(self, context: Context) -> str:
        """
        消息预处理 - 将不同类型的消息转换为AI可理解的格式
        
        Args:
            context: 消息上下文
            
        Returns:
            处理后的消息内容（JSON字符串格式）
        """
        import json
        
        try:
            # 处理商品咨询类型
            if context.type == ContextType.GOODS_INQUIRY or context.type == ContextType.GOODS_SPEC:
                try:
                    goods_info = context.content
                    message = f'商品：{goods_info.get("goods_name")},商品价格：{goods_info.get("goods_price")},商品规格：{goods_info.get("goods_spec")}'
                    return json.dumps([{"type": "text", "text": message}], ensure_ascii=False)
                except Exception as e:
                    self.logger.error(f"处理商品咨询消息失败: {str(e)}")
                    return json.dumps([{"type": "text", "text": "收到商品咨询"}], ensure_ascii=False)
           
            # 处理订单信息类型
            elif context.type == ContextType.ORDER_INFO:
                try:
                    order_info = context.content
                    order_id = order_info.get("order_id")
                    goods_name = order_info.get("goods_name")
                    message = f"订单：{order_id}，商品：{goods_name}"
                    return json.dumps([{"type": "text", "text": message}], ensure_ascii=False)
                except Exception as e:
                    self.logger.error(f"处理订单信息消息失败: {str(e)}")
                    return json.dumps([{"type": "text", "text": "收到订单查询"}], ensure_ascii=False)

            # 文本消息处理
            elif context.type == ContextType.TEXT:
                # 基础文本处理
                return json.dumps([{"type": "text", "text": context.content}], ensure_ascii=False)
                
            # 表情消息处理
            elif context.type == ContextType.EMOTION:
                return json.dumps([{"type": "text", "text": f"表情: {context.content}"}], ensure_ascii=False)
            
            # 图片消息处理
            elif context.type == ContextType.IMAGE:
                return json.dumps([{"type": "text", "text": f"图片: {context.content}"}], ensure_ascii=False)
                
            # 视频消息处理
            elif context.type == ContextType.VIDEO:
                return json.dumps([{"type": "text", "text": f"视频: {context.content}"}], ensure_ascii=False)
                
            # 默认处理
            else:
                self.logger.warning(f"未知消息类型: {context.type}")
                return json.dumps([{"type": "text", "text": str(context.content)}], ensure_ascii=False)
                
        except Exception as e:
            self.logger.error(f"消息预处理失败: {e}")
            return json.dumps([{"type": "text", "text": "消息处理失败"}], ensure_ascii=False)

    @monitor_async_function("ai_message_handler", {"handler": "AIAutoReplyHandler"})
    async def handle(self, context: Context, metadata: Dict[str, Any]) -> bool:
        """处理消息并发送AI回复 - 集成性能监控和超时中断重发"""
        try:
            shop_id = context.kwargs.get('shop_id')
            user_id = context.kwargs.get('user_id')
            from_uid = context.kwargs.get('from_uid')
            username = context.kwargs.get("username")
            nickname = context.kwargs.get("nickname")
            if not all([shop_id, user_id, from_uid]):
                self.logger.error("缺少必要的用户或店铺信息")
                return False

            # 限流检查：仅对正常请求检查（取消重发也受限流影响）
            if self.rate_limiter.is_rate_limited(from_uid):
                from bridge.reply import Reply, ReplyType
                fallback = Reply(ReplyType.TEXT, self._get_random_fallback())
                await self._send_reply(fallback, shop_id, user_id, from_uid)
                self.logger.warning(f"'{username}'用户'{nickname}'({from_uid})已触发限流，发送兜底回复")
                return True

            # 检查是否是取消重发
            pending_info = metadata.get('pending_ai_info')
            if pending_info:
                reply = await self._cancel_and_resend(context, pending_info)
                if reply:
                    await self._send_reply(reply, shop_id, user_id, from_uid)
                    self.logger.info(f"'{username}'取消重发回复用户'{nickname}': {reply.content[:100]}")
                else:
                    from bridge.reply import Reply, ReplyType
                    fallback = Reply(ReplyType.TEXT, self._get_random_fallback())
                    await self._send_reply(fallback, shop_id, user_id, from_uid)
                    self.logger.warning(f"'{username}'取消重发未能获取有效回复，发送兜底回复")
                return True

            try:
                self.logger.info(f"'{username}'收到用户'{nickname}'消息: 消息类型：{context.type},消息内容：{context.content}")
                
                # 计算AI超时时间：扣除人工等待时间
                staff_wait_elapsed = metadata.get('staff_wait_elapsed', 0.0)
                ai_timeout = self.REPLY_TIMEOUT - staff_wait_elapsed
                
                if staff_wait_elapsed > 0:
                    self.logger.info(
                        f"'{username}' AI超时时间扣除人工等待{staff_wait_elapsed}秒，"
                        f"剩余{ai_timeout:.0f}秒"
                    )
                
                reply = await self._get_ai_reply(context, timeout=ai_timeout)
                if reply:
                    await self._send_reply(reply, shop_id, user_id, from_uid)
                    self.logger.info(f"'{username}'回复用户'{nickname}': {reply.content[:100]}")
                else:
                    from bridge.reply import Reply, ReplyType
                    fallback = Reply(ReplyType.TEXT, self._get_random_fallback())
                    await self._send_reply(fallback, shop_id, user_id, from_uid)
                    self.logger.warning(f"'{username}'未能获取AI回复给用户'{nickname}'，发送兜底回复")
            except Exception as e:
                self.logger.error(f"AI回复生成失败: {e}")
                return False
            return True
        except Exception as e:
            self.logger.error(f"AI自动回复处理失败: {e}")
            return False

    async def _get_ai_reply(self, context: Context, timeout: float = None):
        """获取AI Bot回复 - 带超时中断支持"""
        if not self.bot:
            self.logger.warning("AI Bot实例不可用，无法获取回复")
            return None

        try:
            # 预处理消息内容
            processed_content = self._preprocess_message(context)

            # 创建新的context对象，将预处理后的内容传递给bot
            processed_context = Context(
                type=ContextType.TEXT,  # 统一转换为TEXT类型
                content=processed_content,
                channel_type=context.channel_type,
                kwargs=context.kwargs
            )

            if timeout is None:
                timeout = self.REPLY_TIMEOUT

            # 使用专用线程池运行同步的bot.reply方法
            loop = asyncio.get_running_loop()

            # 添加超时控制，防止长时间阻塞
            reply = await asyncio.wait_for(
                loop.run_in_executor(
                    self.executor,  # 使用专用的线程池
                    self.bot.reply,
                    processed_context
                ),
                timeout=timeout
            )

            return reply

        except asyncio.TimeoutError:
            self.logger.error(f"AI回复超时(timeout={timeout}s)")
            return None

        except Exception as e:
            self.logger.error(f"获取AI回复失败: {e}", exc_info=True)
            return None

    async def _cancel_and_resend(self, new_context: Context, pending_info: Dict[str, Any]):
        """
        取消当前AI对话，将新消息追加后重新发起

        Args:
            new_context: 新消息的context（已在consumer层合并+防抖）
            pending_info: 之前AI请求的pending信息
        """
        chat_info = pending_info.get('chat_info')
        original_content = pending_info.get('original_content')
        original_kwargs = pending_info.get('original_kwargs')
        cancel_used_time = pending_info.get('cancel_used_time', 0)

        if not chat_info or not original_content:
            self.logger.warning("取消重发缺少pending信息，降级为普通处理")
            return await self._get_ai_reply(new_context)

        chat_id = chat_info.get('chat_id')
        conversation_id = chat_info.get('conversation_id')

        # 1. 取消当前对话
        cancelled = False
        if hasattr(self.bot, 'cancel_chat') and chat_id and conversation_id:
            cancelled = self.bot.cancel_chat(chat_id, conversation_id)
        if not cancelled:
            self.logger.warning(f"取消对话失败(chat_id={chat_id})，仍然尝试重发")

        # 2. 直接使用新消息的纯文本内容作为追加（已在consumer层合并+防抖完成）
        # original_content 是纯文本格式，所以追加内容也应该是纯文本
        append_content = new_context.content if new_context.type == ContextType.TEXT else str(new_context.content)
        self.logger.info(f"取消重发: 追加新内容 '{append_content[:100]}'")

        # 3. 计算剩余超时：总超时 - 已用时间，保底120秒
        remaining_timeout = max(
            self.REPLY_TIMEOUT - cancel_used_time,
            self.MIN_REPLY_TIMEOUT
        )
        self.logger.info(f"取消重发: 剩余超时 {remaining_timeout:.0f}s (已用 {cancel_used_time:.0f}s)")

        # 4. 用原context重新发起对话（追加新内容）
        resend_context = Context(
            type=ContextType.TEXT,
            content=original_content,
            channel_type=new_context.channel_type,
            kwargs=original_kwargs
        )

        if hasattr(self.bot, '_create_message_and_get_reply'):
            loop = asyncio.get_running_loop()

            def _do_resend():
                return self.bot._create_message_and_get_reply(
                    conversation_id,
                    original_content,
                    resend_context,
                    append_query=append_content,
                    timeout=int(remaining_timeout)
                )

            try:
                reply = await asyncio.wait_for(
                    loop.run_in_executor(self.executor, _do_resend),
                    timeout=remaining_timeout + 5  # 额外5秒给executor本身
                )
                return reply
            except asyncio.TimeoutError:
                self.logger.error(f"取消重发后超时(timeout={remaining_timeout:.0f}s)")
                return None
            except Exception as e:
                error_str = str(e)
                # Conversation occupied (4016): cancel_chat后服务端可能还没完全释放，多次重试
                if "4016" in error_str or "Conversation occupied" in error_str:
                    max_retries = 3
                    for retry in range(max_retries):
                        wait_time = 2 + retry  # 递增等待时间：2s, 3s, 4s
                        self.logger.warning(
                            f"取消重发遇到Conversation occupied，等待{wait_time}秒后重试({retry+1}/{max_retries})"
                        )
                        await asyncio.sleep(wait_time)
                        try:
                            reply = await asyncio.wait_for(
                                loop.run_in_executor(self.executor, _do_resend),
                                timeout=remaining_timeout + 5
                            )
                            self.logger.info(f"Conversation occupied重试成功")
                            return reply
                        except Exception as retry_e:
                            error_str_retry = str(retry_e)
                            if "4016" in error_str_retry or "Conversation occupied" in error_str_retry:
                                if retry < max_retries - 1:
                                    continue  # 继续重试
                                else:
                                    self.logger.error(f"Conversation occupied重试{max_retries}次后仍失败")
                                    return None
                            else:
                                self.logger.error(f"取消重发重试失败: {retry_e}", exc_info=True)
                                return None
                    return None
                else:
                    self.logger.error(f"取消重发失败: {e}", exc_info=True)
                    return None
        else:
            return await self._get_ai_reply(new_context)
        
    async def _send_reply(self, reply, shop_id: str, user_id: str, from_uid: str) -> bool:
        """发送回复消息"""
        try:
            sender = SendMessage(shop_id, user_id)
            
            # 处理不同类型的回复
            if hasattr(reply, '__iter__') and not isinstance(reply, str):
                # 处理多个回复的情况
                for single_reply in reply:
                    success = await self._send_single_reply(single_reply, sender, from_uid)
                    if not success:
                        return False
                return True
            else:
                # 处理单个回复
                return await self._send_single_reply(reply, sender, from_uid)
                
        except Exception as e:
            self.logger.error(f"发送回复失败: {e}")
            return False
    
    async     def _send_single_reply(self, reply, sender, from_uid: str) -> bool:
        """发送单个回复"""
        try:
            from bridge.reply import ReplyType
            
            if hasattr(reply, 'type') and hasattr(reply, 'content'):
                # 处理Reply对象，只处理TEXT类型
                if reply.type == ReplyType.TEXT:
                    result = sender.send_text(from_uid, reply.content)
                else:
                    # 非TEXT类型转为文本发送
                    result = sender.send_text(from_uid, str(reply.content))
                    
            else:
                # 处理字符串类型的回复
                result = sender.send_text(from_uid, str(reply))
            
            if result:
                return True
            else:
                self.logger.error("AI回复发送失败")
                return False
                
        except Exception as e:
            self.logger.error(f"发送单个回复失败: {e}")
            return False

    def reload_rate_limit_config(self):
        """重新从 config 加载限流配置（供 UI 修改后调用）"""
        try:
            from config import config
            rc = config.get_rate_limit_config()
            self.rate_limiter.configure(
                window_size=rc['window_hours'] * 3600,
                max_requests=rc['max_requests']
            )
            self._fallback_reply = rc['fallback_reply']
            self.logger.info(f"限流配置已热更新: 窗口={rc['window_hours']}h, 最大={rc['max_requests']}次, 兜底回复={len(self._fallback_reply) if isinstance(self._fallback_reply, list) else 1}条")
        except Exception as e:
            self.logger.error(f"热更新限流配置失败: {e}")
    

class KeywordTriggerHandler(MessageHandler):
    """关键词触发处理器"""
    
    def __init__(self, keyword_rules: Dict[str, Callable[[Context, Dict[str, Any]], Awaitable[bool]]]):
        """
        初始化关键词触发处理器
        
        Args:
            keyword_rules: 关键词规则字典 {关键词: 处理函数}
        """
        self.keyword_rules = keyword_rules
        self.logger = get_logger()
    
    def can_handle(self, context: Context) -> bool:
        """检查消息是否包含关键词"""
        if context.type != ContextType.TEXT:
            return False
        
        # 确保content是字符串
        if not isinstance(context.content, str):
            return False
            
        message = context.content.lower()
        return any(keyword in message for keyword in self.keyword_rules.keys())
    
    async def handle(self, context: Context, metadata: Dict[str, Any]) -> bool:
        """根据关键词触发相应处理"""
        try:
            # 确保content是字符串
            if not isinstance(context.content, str):
                return False
                
            message = context.content.lower()
            
            for keyword, handler_func in self.keyword_rules.items():
                if keyword in message:
                    self.logger.info(f"触发关键词: {keyword}")
                    return await handler_func(context, metadata)
                    
        except Exception as e:
            self.logger.error(f"关键词触发处理失败: {e}")
            
        return False


class CustomerServiceTransferHandler(MessageHandler):
    """客服转接处理器 - 从数据库读取关键词分组规则"""

    # 用于去掉关键词后的分隔符
    _SEPARATOR_RE = re.compile(r'[,，.;；!！?？、\s]+')
    # 循环匹配最大次数（防止死循环）
    _MAX_LOOP = 10
    # 回复分隔符（用于合并多个回复）
    _REPLY_SEPARATOR = "\n"

    def __init__(self, keyword_rules: List[Dict[str, Any]] = None):
        """
        初始化客服转接处理器

        Args:
            keyword_rules: 关键词规则列表，每个规则格式:
                {'keywords': [...], 'reply': '...', 'is_transfer': 0/1, 'pass_to_ai': 0/1, 'group_name': '...'}
                如果为None则从数据库加载
        """
        self.logger = get_logger()
        self._load_rules(keyword_rules)

    def _load_rules(self, keyword_rules: List[Dict[str, Any]] = None):
        """加载关键词规则"""
        if keyword_rules is None:
            try:
                from database.db_manager import db_manager
                keyword_rules = db_manager.get_keyword_reply_rules()
            except Exception as e:
                self.logger.error(f"从数据库加载关键词规则失败: {e}")
                keyword_rules = []

        self.keyword_rules = keyword_rules
        # 构建扁平化查找表: keyword_text -> rule_dict
        self._keyword_map = {}
        for rule in self.keyword_rules:
            for kw in rule.get('keywords', []):
                self._keyword_map[kw.lower()] = rule
        self.logger.info(f"已加载 {len(self.keyword_rules)} 个关键词分组规则，"
                         f"共 {len(self._keyword_map)} 个关键词")

    def reload_rules(self):
        """重新从数据库加载规则（供UI修改后调用）"""
        self._load_rules(None)

    def can_handle(self, context: Context) -> bool:
        """检查是否需要转接人工客服或自动回复"""
        if context.type != ContextType.TEXT:
            return False

        if not isinstance(context.content, str):
            return False

        message = context.content.lower()
        return any(kw in message for kw in self._keyword_map)

    async def handle(self, context: Context, metadata: Dict[str, Any]) -> bool:
        """根据匹配到的关键词规则进行处理，支持单条消息内循环匹配、去重和回复合并"""
        try:
            if not isinstance(context.content, str):
                return False

            shop_id = context.kwargs.get('shop_id')
            user_id = context.kwargs.get('user_id')
            from_uid = context.kwargs.get('from_uid')

            if not all([shop_id, user_id, from_uid]):
                return False

            original_content = context.content  # 保存原始消息
            remaining = context.content.lower()
            transferred = False  # 是否已执行转人工
            loop_count = 0
            
            # 收集所有回复内容，用于合并发送
            collected_replies = []
            # 记录本条消息内已匹配的分组（单条消息内不重复）
            matched_groups_in_message = set()

            while remaining.strip() and loop_count < self._MAX_LOOP:
                loop_count += 1

                # 查找匹配的规则（优先匹配最长的关键词）
                matched_rule = None
                matched_keyword = None
                max_len = 0
                for kw_text, rule in self._keyword_map.items():
                    if kw_text in remaining and len(kw_text) > max_len:
                        # 检查该分组是否在本条消息内已匹配过
                        group_name = rule.get('group_name', '')
                        if group_name in matched_groups_in_message:
                            self.logger.debug(f"分组 '{group_name}' 在本条消息内已匹配过，跳过")
                            continue
                        matched_rule = rule
                        matched_keyword = kw_text
                        max_len = len(kw_text)

                if not matched_rule:
                    # 没有更多关键词匹配了
                    break

                group_name = matched_rule.get('group_name', '')
                self.logger.info(f"关键词匹配(第{loop_count}轮): '{matched_keyword}' -> 分组 '{group_name}'")

                # 收集回复内容（如果有）
                reply_text = matched_rule.get('reply')
                if reply_text:
                    collected_replies.append(reply_text)
                    self.logger.debug(f"收集回复: '{reply_text[:50]}...'")

                # 记录本条消息内已匹配的分组
                if group_name:
                    matched_groups_in_message.add(group_name)

                # 如果需要转人工
                if matched_rule.get('is_transfer'):
                    await self._transfer_to_human(context, shop_id, user_id, from_uid)
                    transferred = True

                # 去掉已匹配的关键词和紧邻的分隔符，检查剩余内容
                remaining = remaining.replace(matched_keyword, '', 1)
                remaining = self._SEPARATOR_RE.sub('', remaining, count=1)
                remaining = remaining.strip()

            # 统一发送收集到的所有回复
            if collected_replies:
                merged_reply = self._REPLY_SEPARATOR.join(collected_replies)
                sender = SendMessage(shop_id, user_id)
                sender.send_text(from_uid, merged_reply)
                self.logger.info(f"关键词自动回复: 合并发送 {len(collected_replies)} 条回复给 {from_uid}")

            # 所有循环结束后：
            if transferred:
                # 已经转人工了，不管剩余内容
                return True

            if remaining:
                # 还有未匹配的内容 → 传给AI处理（保留完整原始消息）
                self.logger.info(f"关键词匹配完成，原始消息传给AI处理")
                # 保持原始消息不变，让AI处理器处理完整内容
                return False

            # 全部内容都被关键词匹配处理完了
            return True

        except Exception as e:
            self.logger.error(f"关键词规则处理失败: {e}")
            return False

    async def _transfer_to_human(self, context: Context, shop_id: str, user_id: str, from_uid: str) -> bool:
        """转接到人工客服"""
        try:
            sender = SendMessage(shop_id, user_id)
            cs_list = sender.getAssignCsList()
            my_cs_uid = f"cs_{shop_id}_{user_id}"

            if cs_list and isinstance(cs_list, dict):
                # 过滤掉自己
                available_cs_uids = [uid for uid in cs_list.keys() if uid != my_cs_uid]

                if available_cs_uids:
                    cs_uid = available_cs_uids[0]
                    target_cs = cs_list[cs_uid]
                    cs_name = target_cs.get('username', '客服')

                    transfer_result = sender.move_conversation(from_uid, cs_uid)

                    if transfer_result and transfer_result.get('success'):
                        self.logger.info(f"会话已成功转接给 {cs_name} ({cs_uid})")
                        return True
                    else:
                        self.logger.error("会话转接失败")
                else:
                    self.logger.warning("没有其他可用的客服进行转接")
                    sender.send_text(from_uid, "抱歉，当前没有其他客服在线，请您稍后再试。")

            return False

        except Exception as e:
            self.logger.error(f"客服转接处理失败: {e}")
            return False


class BusinessHoursHandler(MessageHandler):
    """营业时间处理器"""
    
    def __init__(self, business_hours: Dict[str, str] = None):
        """
        初始化营业时间处理器
        
        Args:
            business_hours: 营业时间配置 {'start': '08:00', 'end': '23:00'}
        """
        self.business_hours = business_hours or {'start': '08:00', 'end': '23:00'}
        self.logger = get_logger()
    
    def can_handle(self, context: Context) -> bool:
        """检查是否在非营业时间"""
        return not self._is_business_hours()
    
    async def handle(self, context: Context, metadata: Dict[str, Any]) -> bool:
        """处理非营业时间的消息"""
        try:
            shop_id = context.kwargs.get('shop_id')
            user_id = context.kwargs.get('user_id')
            from_uid = context.kwargs.get('from_uid')
            
            if not all([shop_id, user_id, from_uid]):
                return False
            
            current_time = datetime.now().strftime('%H:%M:%S')
            start_time = self.business_hours['start']
            end_time = self.business_hours['end']
            
            reply = (f"您好！当前时间是 {current_time}，我们的营业时间是 {start_time}-{end_time}。"
                    f"现在是非营业时间，您可以先留言，我们会在营业时间内尽快回复您。")
            
            sender = SendMessage(shop_id, user_id)
            sender.send_text(from_uid, reply)
            self.logger.info(f"非营业时间自动回复:回复 {reply} 给 {from_uid}")
            return True
            
        except Exception as e:
            self.logger.error(f"营业时间处理失败: {e}")
            
        return False
    
    def _is_business_hours(self) -> bool:
        """检查当前是否在营业时间内"""
        now = datetime.now().time()
        start_time = datetime.strptime(self.business_hours['start'], '%H:%M').time()
        end_time = datetime.strptime(self.business_hours['end'], '%H:%M').time()
        
        return start_time <= now <= end_time


# 便捷函数：创建预配置的处理器
def create_ai_handler(bot=None, enable_fallback: bool = True, max_workers: int = 5) -> AIAutoReplyHandler:
    """
    创建AI自动回复处理器

    Args:
        bot: AI Bot实例，如果为None会自动创建CozeBot
        enable_fallback: 是否启用规则回复作为后备
        max_workers: 线程池最大工作线程数
    """
    return AIAutoReplyHandler(bot=bot, enable_fallback=enable_fallback, max_workers=max_workers)


def create_coze_ai_handler(max_workers: int = 5) -> AIAutoReplyHandler:
    """创建基于CozeBot的AI回复处理器"""
    try:
        from Agent.bot_factory import create_bot
        bot = create_bot()
        return AIAutoReplyHandler(bot=bot, enable_fallback=True, max_workers=max_workers)
    except Exception as e:
        return AIAutoReplyHandler(bot=None, enable_fallback=True, max_workers=max_workers)





def handler_chain(use_ai: bool = True, businessHours: Dict[str, str] = None) -> List[MessageHandler]:
    """
    创建完整的处理器链

    Args:
        use_ai: 是否使用AI回复处理器
    """
    handlers = [
        BusinessHoursHandler(business_hours=businessHours),                     # 营业时间检查
        CustomerServiceTransferHandler()           # 关键词匹配 + 客服转接（从数据库加载规则）
    ]

    # 添加AI处理器（处理所有其他消息类型）
    if use_ai:
        handlers.append(create_ai_handler())
    else:
        handlers.append(AIAutoReplyHandler(bot=None, enable_fallback=True))

    return handlers

