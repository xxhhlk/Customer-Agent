# 消息处理模块
import json
import asyncio
from websockets import exceptions as ws_exceptions
from typing import Optional, Any, TYPE_CHECKING
from bridge.context import Context, ContextType, ChannelType
from Channel.pinduoduo.message_rules import Action, Origin, effective_action
from Channel.pinduoduo.pdd_message import PDDChatMessage
from database import db_manager
from utils.logger_loguru import get_logger

if TYPE_CHECKING:
    from core.connection_status import ConnectionStatusManager


class MessageHandlerMixin:
    """消息处理 Mixin"""

    # Attributes provided by PDDChannel host class
    channel_name: str
    logger: Any
    status_manager: "ConnectionStatusManager"
    businessHours: Optional[dict]

    async def _setup_message_consumer(self, queue_name: str):
        """设置消息消费者和处理器链"""
        from Message.core.enhanced_consumer import enhanced_message_consumer_manager
        from Message import queue_manager, handler_chain
        from Agent.CustomerAgent.agent import CustomerAgent

        try:
            existing_consumer = enhanced_message_consumer_manager.get_consumer(queue_name)
            if existing_consumer:
                self.logger.info(f"消费者 {queue_name} 已存在，先停止并重新创建以绑定当前事件循环")
                try:
                    await enhanced_message_consumer_manager.stop_consumer(queue_name)
                except Exception as e:
                    self.logger.warning(f"停止旧消费者失败: {queue_name}, {e}")
                try:
                    queue_manager.recreate_queue(queue_name)
                except Exception as e:
                    self.logger.warning(f"重新创建队列失败: {queue_name}, {e}")

            consumer = enhanced_message_consumer_manager.create_consumer(queue_name, max_concurrent=10)

            try:
                from core.di_container import container
                bot = container.get(CustomerAgent)
            except Exception:
                bot = CustomerAgent()
            handlers = handler_chain(use_ai=True, businessHours=self.businessHours, bot=bot)
            for handler in handlers:
                consumer.add_handler(handler)

            await enhanced_message_consumer_manager.start_consumer(queue_name)
            self.logger.debug(f"消息消费者已启动: {queue_name}")

        except Exception as e:
            self.logger.error(f"设置消息消费者失败: {e}")
            raise

    async def _process_websocket_message(self, message: str, shop_id: str, user_id: str, username: str, queue_name: str):
        """处理单条WebSocket消息"""
        try:
            if not message or not message.strip():
                self.logger.debug(f"收到空消息，跳过处理: {shop_id}-{username}")
                return

            message_data = json.loads(message)
            msg_type = message_data.get("message", {}).get("type", "unknown")
            from_uid_log = message_data.get("message", {}).get("from_uid", "unknown")
            self.logger.debug(f"收到消息: type={msg_type}, from_uid={from_uid_log}, shop_id={shop_id}")

            # 当收到客服端发送的视频消息（mall_cs + type=14），打印完整结构用于对比
            if msg_type == 14:
                msg_body = message_data.get("message", {})
                self.logger.info(f"[MALL_CS_VIDEO] ===== 收到客服端视频消息（对比用）=====")
                self.logger.info(f"[MALL_CS_VIDEO] from_role={msg_body.get('from', {}).get('role')}, "
                                f"to_uid={msg_body.get('to', {}).get('uid')}, "
                                f"content={str(msg_body.get('content', ''))[:120]}...")
                self.logger.info(f"[MALL_CS_VIDEO] message.info={json.dumps(msg_body.get('info'), ensure_ascii=False, default=str)}")
                self.logger.info(f"[MALL_CS_VIDEO] 完整 message keys: {list(msg_body.keys())}")
                self.logger.info(f"[MALL_CS_VIDEO] 完整 message: {json.dumps(msg_body, ensure_ascii=False, default=str)}")

            try:
                pdd_message = PDDChatMessage(message_data)
            except Exception as pdd_error:
                self.logger.error(f"创建PDD消息对象失败: {shop_id}-{username}, 错误: {pdd_error}")
                return

            try:
                context = await self._convert_to_context(pdd_message, shop_id, user_id, username)
                if not context:
                    self.logger.debug(f"消息转换失败，跳过处理: {shop_id}-{username}")
                    return
            except Exception as ctx_error:
                self.logger.error(f"转换Context失败: {shop_id}-{username}, 错误: {ctx_error}")
                return

            if context:
                # === 持久化入站消息 ===
                try:
                    from services.message_persistence import message_persistence_service
                    msg_dict = message_persistence_service.save_inbound_message(context)
                    if msg_dict:
                        message_persistence_service.notify_new_message(msg_dict)
                except Exception as e:
                    self.logger.warning(f"持久化入站消息失败: {e}")

                await self._dispatch_by_action(context, shop_id, user_id, queue_name, pdd_message)
            else:
                self.logger.warning("消息转换失败，跳过处理")

        except json.JSONDecodeError:
            self.logger.error(f"JSON解析失败: {message}")
        except Exception as e:
            self.logger.error(f"处理WebSocket消息失败: {e}")

    def _resolve_action(self, pdd_message: PDDChatMessage) -> Action:
        """取解析层判定出的动作；缺失或非法时降级为 UNKNOWN。"""
        action = getattr(pdd_message, "action", None)
        if isinstance(action, Action):
            return action
        try:
            return Action(action)
        except ValueError:
            return Action.UNKNOWN

    async def _dispatch_by_action(
        self,
        context: Context,
        shop_id: str,
        user_id: str,
        queue_name: str,
        pdd_message: PDDChatMessage,
    ) -> None:
        """按动作分派。每条消息都有归宿，不存在静默丢弃的分支。"""
        raw_action = self._resolve_action(pdd_message)
        action = effective_action(raw_action)
        origin = getattr(pdd_message, "origin", None)
        origin_value = getattr(origin, "value", "unknown")
        pdd_type = getattr(pdd_message, "pdd_type", None)
        sub_type = getattr(pdd_message, "pdd_sub_type", None)
        template = getattr(pdd_message, "template_name", None)

        # 铁律：未识别的组合降级为 CONTEXT_ONLY 的同时必须告警。
        # 只降级不告警，等于让动作表的缺项悄悄消失，后续无从补表。
        if raw_action is Action.UNKNOWN:
            self.logger.warning(
                f"unknown message action, degraded to context_only: "
                f"origin={origin_value}, type={pdd_type}, sub_type={sub_type}, "
                f"template={template}"
            )

        # 客服侧（MERCHANT）消息：先完成既有的人工回复处理
        # （staff_reply_event 通知 + 上下文缓存），这是 60s cooldown 与
        # AI 上下文的事件源，不能因动作分类而跳过；随后照常按动作分流
        # （动作表保证客服侧不产生 REPLY，不会与人工抢答）。
        if origin is Origin.MERCHANT:
            self._handle_staff_reply(context)

        if action is Action.REPLY:
            from Message import put_message
            msg_id = await put_message(queue_name, context)
            self.logger.debug(f"消息已入队: {queue_name}, ID: {msg_id}, 类型: {context.type}")
            return

        if action is Action.HANDOFF:
            await self._handle_immediate_message(context, shop_id, user_id)
            return

        if action is Action.CONTEXT_ONLY:
            # 买家侧 CONTEXT_ONLY（订单卡 / 用户来源等）：不回复、不丢弃
            self.logger.debug(
                f"context only: origin={origin_value}, type={pdd_type}, "
                f"template={template}, content={str(context.content)[:120]}"
            )
            return

        if action is Action.IGNORE:
            self.logger.debug(
                f"message ignored: origin={origin_value}, type={pdd_type}, template={template}"
            )
            return

        # OBSERVE 及任何未预期分支：只记录，绝不丢弃
        await self._handle_observe(context, origin_value, pdd_type, template)

    def _handle_staff_reply(self, context: Context) -> None:
        """客服侧消息的既有处理：通知人工回复事件 + 缓存消息供 AI 上下文。

        注意：人工客服消息的 from_uid 是店铺侧账号，to_uid 才是买家。
        """
        buyer_uid = context.kwargs.to_uid
        if buyer_uid:
            try:
                from Message.handlers.staff_reply_event import staff_reply_event_manager
                staff_reply_event_manager.notify_staff_reply(buyer_uid)
            except Exception as e:
                self.logger.error(f"通知人工回复事件失败: {e}")
        else:
            self.logger.warning("客服消息缺少 to_uid，无法定位买家")

        # 缓存客服消息，供 AI 回复时作为上下文；结构化卡片先摘要成一行，
        # 避免原始 JSON 直接进 AI 上下文。
        if context.content and buyer_uid:
            from Message.handlers.staff_message_cache import staff_message_cache
            staff_message_cache.add_message(
                buyer_uid, self._summarize_content(context.content)
            )

    @staticmethod
    def _summarize_content(content: str) -> str:
        """把结构化卡片（JSON）压成一行可读文本；纯文本原样返回。"""
        if not isinstance(content, str):
            return str(content)
        stripped = content.strip()
        if not stripped.startswith("{"):
            return content
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return content
        if not isinstance(parsed, dict):
            return content
        parts = []
        for key in ("title", "text", "sub_title", "description",
                    "goods_name", "order_id", "content"):
            value = parsed.get(key)
            if value and str(value) not in parts:
                parts.append(str(value))
        return "，".join(parts) if parts else content

    @staticmethod
    def _extract_auth_result(content) -> str:
        """从 auth 消息内容里取 result。

        内容可能是 dict，也可能已被归一化为 JSON 字符串，两种都要支持。
        """
        if isinstance(content, dict):
            return str(content.get("result"))
        if isinstance(content, str):
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError:
                return content[:60]
            if isinstance(parsed, dict):
                return str(parsed.get("result"))
        return "unknown"

    async def _handle_observe(
        self,
        context: Context,
        origin: str,
        pdd_type,
        template,
    ) -> None:
        """只观察，不参与对话。

        type=30（system_push）实测内容为「账户在别处登录 请刷新重登。」，
        属于需要运维可见的信号，因此提升到 WARNING。
        """
        # auth 的连接鉴权结果保留 INFO 可见性（排查登录问题需要）。
        # 注意 _convert_to_context 已把 dict 归一化成 JSON 字符串，
        # 因此这里必须解析字符串——原实现只判 dict，日志实际从未打印。
        if context.type == ContextType.AUTH:
            username = getattr(context.kwargs, "username", "") or ""
            self.logger.info(
                f"{username} auth result: {self._extract_auth_result(context.content)}"
            )
            return

        message = (
            f"observe: origin={origin}, type={pdd_type}, "
            f"template={template}, ctx={context.type}"
        )
        # type=30 实测为「账户在别处登录 请刷新重登。」，需运维可见
        if pdd_type == 30 and context.content:
            self.logger.warning(f"{message}, content={str(context.content)[:120]}")
        elif context.type == ContextType.TRANSFER:
            # 默认日志级别是 INFO，转接若记 debug 等于没记；
            # 而「转接通知可排查」正是客服侧仍然要解析消息的理由之一。
            self.logger.info(f"{message}, content={str(context.content)[:120]}")
        else:
            self.logger.debug(message)

    async def _handle_immediate_message(self, context: Context, shop_id: str, user_id: str):
        """处理 HANDOFF 动作：目前只有买家侧会话转接。

        AUTH 归入 OBSERVE、WITHDRAW 归入 IGNORE，都不再到这条路径，
        因此这里不再保留它们的分支，避免出现永不执行的死代码。
        """
        recipient_uid = getattr(context.kwargs, "from_uid", None)
        if isinstance(context.kwargs, dict):
            recipient_uid = recipient_uid or context.kwargs.get("from_uid")
        recipient_uid = recipient_uid or ""
        try:
            from Channel.pinduoduo.utils.API.send_message import SendMessage

            def _send_notice() -> None:
                # 构造也要放进工作线程：SendMessage -> BaseRequest.__init__ ->
                # _init_account_info() 会同步读 cookie 缓存与数据库，
                # 在事件循环线程里构造会阻塞整条连接的收发。
                SendMessage(shop_id, user_id).send_text(recipient_uid, "[玫瑰]")

            if context.type == ContextType.TRANSFER:
                self.logger.info(f"转接消息: {context.content}")
                await asyncio.to_thread(_send_notice)
            else:
                self.logger.debug(f"handoff: unhandled type {context.type}")
        except Exception as e:
            self.logger.error(f"立即处理消息失败: {e}")

    async def _convert_to_context(self, pdd_message: PDDChatMessage, shop_id: str, user_id: str, username: str) -> Context:
        """将拼多多消息转换为Context格式"""
        shop_info = await asyncio.to_thread(db_manager.get_shop, self.channel_name, shop_id)
        shop_name = shop_info.get("shop_name", "") if shop_info else ""

        context_type = pdd_message.user_msg_type

        content = pdd_message.content
        if isinstance(content, dict):
            content = json.dumps(content, ensure_ascii=False)
        elif content is None:
            content = ""
        else:
            content = str(content)

        context = Context.create_pinduoduo_context(
            content=content,
            msg_id=str(pdd_message.msg_id) if pdd_message.msg_id is not None else "",
            from_user=str(pdd_message.from_user) if pdd_message.from_user is not None else "",
            from_uid=str(pdd_message.from_uid) if pdd_message.from_uid is not None else "",
            to_user=str(pdd_message.to_user) if pdd_message.to_user is not None else "",
            to_uid=str(pdd_message.to_uid) if pdd_message.to_uid is not None else "",
            nickname=str(pdd_message.nickname) if pdd_message.nickname is not None else "",
            timestamp=pdd_message.timestamp,
            user_msg_type=pdd_message.user_msg_type,
            shop_id=str(shop_id),
            user_id=str(user_id),
            username=str(username),
            shop_name=str(shop_name),
            raw_data=pdd_message.raw_data,
            channel_type=ChannelType.PINDUODUO,
            origin=getattr(getattr(pdd_message, "origin", None), "value", None),
            action=getattr(getattr(pdd_message, "action", None), "value", None),
            pdd_type=getattr(pdd_message, "pdd_type", None),
            pdd_sub_type=getattr(pdd_message, "pdd_sub_type", None),
            template_name=getattr(pdd_message, "template_name", None),
        )
        return context


__all__ = ['MessageHandlerMixin']
