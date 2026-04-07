import time
import json
from Agent.bot import Bot
from bridge.context import ContextType, Context
from bridge.reply import Reply, ReplyType
from utils.logger import get_logger
from config import config
from cozepy import Coze, TokenAuth
from Agent.CozeAgent.user_session import UserSessionManager
from Agent.CozeAgent.conversation_manager import ConversationManager


class CozeBot(Bot):
    def __init__(self):
        super().__init__()
        self.logger = get_logger("CozeBot")
        self.token = config.get("coze_token")
        self.bot_id = config.get("coze_bot_id")
        # 初始化Coze客户端
        self.coze_client = Coze(
            auth=TokenAuth(token=self.token),
            base_url=config.get("coze_api_base")
        )
        # 初始化会话管理组件
        self.session_manager = UserSessionManager()
        self.conv_manager = ConversationManager(
            coze_client=self.coze_client,
            session_manager=self.session_manager
        )

    def reply(self, context: Context):
        try:
            # 统一获取用户ID
            from_id = context.kwargs.get("from_uid")
            shop_id = context.kwargs.get("shop_id")
            user_id = f"{shop_id}_{from_id}"

            # 直接使用预处理后的消息内容
            query = context.content

            # 获取或创建会话（使用数据库管理）
            conversation_id = self.session_manager.get_session(user_id)
            if not conversation_id:
                if not (conversation_id := self.conv_manager.create_conversation(user_id)):
                    self.logger.error("会话创建失败")
                    return None

            # 创建消息并获取回复
            return self._create_message_and_get_reply(conversation_id, query, context)

        except Exception as e:
            self.logger.error(f"处理消息异常: {str(e)}", exc_info=True)
            return None

    def cancel_chat(self, chat_id: str, conversation_id: str) -> bool:
        """取消进行中的Coze对话，并等待确认对话已终止"""
        try:
            self.coze_client.chat.cancel(
                conversation_id=conversation_id,
                chat_id=chat_id,
            )
            self.logger.info(f"已发送取消对话请求 chat_id={chat_id}")

            # 轮询等待确认chat确实终止，避免新create时遇到 Conversation occupied
            from cozepy.chat import ChatStatus
            max_retries = 30  # 增加到30次，最多等待15秒
            for i in range(max_retries):
                time.sleep(0.5)
                try:
                    chat = self.coze_client.chat.retrieve(
                        conversation_id=conversation_id, chat_id=chat_id
                    )
                    if chat.status != ChatStatus.IN_PROGRESS:
                        self.logger.info(f"对话已终止 chat_id={chat_id}, status={chat.status}")
                        # 额外等待，确保服务端完全释放conversation
                        time.sleep(1)
                        return True
                except Exception as retrieve_err:
                    # retrieve 失败可能意味着 chat 已被清除
                    self.logger.info(f"对话retrieve异常(可能已终止) chat_id={chat_id}: {retrieve_err}")
                    time.sleep(1)
                    return True

            # 轮询超时后，额外等待更长时间
            self.logger.warning(f"取消对话后轮询超时，额外等待3秒 chat_id={chat_id}")
            time.sleep(3)
            return True

        except Exception as e:
            self.logger.error(f"取消对话异常: {e}")
            return False

    def _create_message_and_get_reply(self, conversation_id, query, context,
                                       append_query: str = None, timeout: int = 165):
        """
        创建消息并获取回复（create+retrieve轮询，支持超时和取消）

        Args:
            conversation_id: Coze会话ID
            query: 主要查询内容（可能是纯文本或JSON格式）
            context: 消息上下文
            append_query: 追加内容（纯文本格式，取消重发时使用）
            timeout: 轮询超时时间（秒）
        """
        try:
            # 如果有追加内容，合并到query中
            if append_query:
                # query 可能是 JSON 格式或纯文本
                if query.startswith('['):
                    # query 是 JSON 格式，提取文本后合并
                    try:
                        query_data = json.loads(query)
                        query_text = ""
                        for item in query_data:
                            if item.get("type") == "text":
                                query_text += item.get("text", "") + "\n"
                        query_text = query_text.strip()
                        # 合并追加内容
                        merged_text = query_text + "\n" + append_query
                        query = json.dumps([{"type": "text", "text": merged_text}], ensure_ascii=False)
                        self.logger.debug(f"合并追加内容(JSON): {merged_text[:100]}")
                    except Exception as e:
                        self.logger.warning(f"JSON解析失败，使用简单拼接: {e}")
                        query = query + "\n" + append_query
                else:
                    # query 是纯文本，直接合并
                    merged_text = query + "\n" + append_query
                    query = json.dumps([{"type": "text", "text": merged_text}], ensure_ascii=False)
                    self.logger.debug(f"合并追加内容(文本): {merged_text[:100]}")

            message = self.coze_client.conversations.messages.create(
                conversation_id=conversation_id,
                content=query,
                role="user",
                content_type="object_string"
            )
            self.logger.debug(f"消息已创建: {message.id}")

            # 获取用户ID
            user_id = context.kwargs.get("from_uid")

            # 创建chat（立即返回chat_id）
            chat = self.coze_client.chat.create(
                conversation_id=conversation_id,
                bot_id=self.bot_id,
                user_id=user_id,
                additional_messages=[message],
                auto_save_history=True
            )

            # 立即记录chat_info到context，供外部取消使用
            context.kwargs.setdefault("_coze_chat_info", {})["chat_id"] = chat.id
            context.kwargs["_coze_chat_info"]["conversation_id"] = conversation_id
            self.logger.debug(f"对话已创建: chat_id={chat.id}")

            # 轮询等待结果（模仿create_and_poll的逻辑）
            from cozepy.chat import ChatStatus
            start = time.time()
            while chat.status == ChatStatus.IN_PROGRESS:
                if time.time() - start > timeout:
                    # 超时，取消对话
                    self.coze_client.chat.cancel(
                        conversation_id=conversation_id, chat_id=chat.id
                    )
                    self.logger.warning(f"对话超时({timeout}s)，已取消 chat_id={chat.id}")
                    return None

                time.sleep(1)
                chat = self.coze_client.chat.retrieve(
                    conversation_id=conversation_id, chat_id=chat.id
                )

            context.kwargs["_coze_chat_info"]["reply_time"] = time.time()

            # 检查chat状态
            if chat.status == ChatStatus.CANCELED:
                self.logger.info(f"对话已被取消 chat_id={chat.id}")
                return None
            if chat.status == ChatStatus.FAILED:
                error_msg = ""
                if chat.last_error:
                    error_msg = f" code={chat.last_error.code}, msg={chat.last_error.msg}"
                self.logger.error(f"对话失败 chat_id={chat.id}{error_msg}")
                return None

            # 获取回复消息
            messages = self.coze_client.chat.messages.list(
                conversation_id=conversation_id, chat_id=chat.id
            )
            if messages:
                for msg in messages:
                    if msg.type.value == "answer" and msg.content_type.value == "text":
                        return Reply(ReplyType.TEXT, msg.content)

            return None
        except Exception as e:
            self.logger.error(f"消息处理失败: {str(e)}")
            return None